#!/usr/bin/env python3
"""Bounded external supervision of owned Prime sessions (POSIX hosts)."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast


def _load_py_module(name: str, path: Path) -> types.ModuleType:
    """Load a sibling/repo module by path without importing the heavy package root."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load module {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/typing see a stable module name.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_state() -> types.ModuleType:
    """Reuse the workflow contract module instead of duplicating its rules."""
    path = Path(__file__).resolve().parent / "prime_state.py"
    return _load_py_module("prime_state_contracts", path)


def load_prime_settings() -> types.ModuleType:
    """Reuse the one Prime settings-precedence contract, path-loaded (stdlib only).

    Controller and worker launches must enforce the SAME one-shot policy, so the
    merge rules and the policy floor live in one module instead of a copy here.
    """
    existing = sys.modules.get("verdict.orchestration.prime_settings")
    if existing is not None:
        return existing
    path = (
        Path(__file__).resolve().parent.parent / "verdict" / "orchestration" / "prime_settings.py"
    )
    return _load_py_module("verdict.orchestration.prime_settings", path)


def load_controller_launch() -> types.ModuleType:
    """Load BOD-156 contracts under canonical ``verdict.controller_launch``.

    Path-loading under a private alias created a second ``ControllerLaunchError``
    class when ``controller_selection`` imported the package module. The CLI
    must catch the same class the selector raises, so register under the
    canonical name (and reuse it when already imported).
    """
    existing = sys.modules.get("verdict.controller_launch")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent.parent / "verdict" / "controller_launch.py"
    if "verdict" not in sys.modules:
        pkg = types.ModuleType("verdict")
        pkg.__path__ = [str(path.parent)]
        sys.modules["verdict"] = pkg
    return _load_py_module("verdict.controller_launch", path)


def load_controller_selection() -> types.ModuleType:
    """Load BOD-156 selector under canonical ``verdict.controller_selection``.

    ``controller_selection`` imports contracts from ``verdict.controller_launch``.
    Loading it under a private alias creates a second module/class graph, so
    exceptions and dataclass types no longer have identity with the canonical
    package modules.  Reuse the already-loaded canonical module and otherwise
    bootstrap the lightweight package exactly as ``load_controller_launch`` does.
    """
    existing = sys.modules.get("verdict.controller_selection")
    if existing is not None:
        return existing
    path = Path(__file__).resolve().parent.parent / "verdict" / "controller_selection.py"
    package = sys.modules.get("verdict")
    if package is None:
        package = types.ModuleType("verdict")
        package.__path__ = [str(path.parent)]
        sys.modules["verdict"] = package
    elif not hasattr(package, "__path__"):
        package.__path__ = [str(path.parent)]
    return _load_py_module("verdict.controller_selection", path)


STATE = load_state()
CL = load_controller_launch()
# Lazy-load selection: controller_selection pulls httpx via cost/capability.
# Bare ``python3 scripts/prime_supervisor.py --help`` must work without extras.
_CS: types.ModuleType | None = None


def _cs() -> types.ModuleType:
    """Load ``verdict.controller_selection`` on first use."""
    global _CS
    if _CS is None:
        _CS = load_controller_selection()
    return _CS


if TYPE_CHECKING:
    from verdict.controller_launch import (
        ControllerLaunchDecision,
        ControllerLaunchError,
        ControllerMission,
        OperatorOverride,
        PersistedAuthoritativeDecision,
        PrimeLaunchTarget,
        decide_controller_launch,
        observe_owned_controller_identity,
        verify_observed_controller_identity,
    )
    # ControllerSelectionHooks / InjectableControllerSelector / select_controller_launch
    # are exposed lazily via __getattr__ / select_controller_launch() so --help stays light.
else:
    ControllerLaunchDecision = CL.ControllerLaunchDecision
    ControllerLaunchError = CL.ControllerLaunchError
    ControllerMission = CL.ControllerMission
    OperatorOverride = CL.OperatorOverride
    PersistedAuthoritativeDecision = CL.PersistedAuthoritativeDecision
    PrimeLaunchTarget = CL.PrimeLaunchTarget
    decide_controller_launch = CL.decide_controller_launch
    observe_owned_controller_identity = CL.observe_owned_controller_identity
    verify_observed_controller_identity = CL.verify_observed_controller_identity


def select_controller_launch(*args: Any, **kwargs: Any) -> Any:
    """Lazy wrapper so import-time ``--help`` does not pull selection deps."""
    return _cs().select_controller_launch(*args, **kwargs)


def __getattr__(name: str) -> Any:
    """Expose CS aliases for tests without eager selection import."""
    if name == "CS":
        return _cs()
    if name == "ControllerSelectionHooks":
        return _cs().ControllerSelectionHooks
    if name == "InjectableControllerSelector":
        return _cs().InjectableControllerSelector
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Test/production injection points for automatic Verdict selection. When set,
# automatic mode (and override mode) call these instead of requiring a
# pre-authored decision file. Unit tests assign doubles here.
CONTROLLER_SELECTOR: Any | None = None
CONTROLLER_SELECTION_HOOKS: Any | None = None
CONTROLLER_SESSION_STATE: Any | None = None
# Production factory injection seam. Tests assign a double that returns a
# ProductionControllerSelectionBundle. Production default builds genuine deps.
CONTROLLER_SELECTION_FACTORY: Any | None = None
# Last production bundle used by resolve (compiled prompt artifacts).
_LAST_PRODUCTION_SELECTION_BUNDLE: Any | None = None

# ---------------------------------------------------------------------------
# Multi-story admission flag (BOD-157 / ADR-037)
# ---------------------------------------------------------------------------
# Test injection point: override to supply a custom evaluator.
# Signature: (story_id, labels, deps, main_sha, footprint, admission_state, config, pressure)
#            -> AdmissionDecision
ADMISSION_EVALUATOR: Any | None = None

_MULTI_STORY_ENV = "VERDICT_MULTI_STORY"


def _multi_story_enabled() -> bool:
    """Return True only when VERDICT_MULTI_STORY is explicitly 'on'.

    Unknown/invalid values are treated as OFF with a warning log line.
    """
    raw = os.environ.get(_MULTI_STORY_ENV, "").strip().lower()
    if raw == "on":
        return True
    if raw and raw != "off":
        print(
            json.dumps(
                {
                    "warning": "VERDICT_MULTI_STORY has unrecognised value; treating as OFF",
                    "value": os.environ.get(_MULTI_STORY_ENV, ""),
                }
            ),
            flush=True,
        )
    return False


def _read_story_metadata(state: Path) -> dict[str, Any]:
    """Read story metadata from the supervisor's existing state files.

    Sources (in priority order):
    1. checkpoint.json — contains issue id, write_paths (from OpenSpec), labels
    2. mission JSON — contains story_id
    3. Fall back to state directory name as story_id

    Anything missing stays unknown, which fails closed (WAIT / SERIALIZE).
    Never invents a footprint.
    """
    meta: dict[str, Any] = {
        "story_id": f"supervisor-{state.name}",
        "labels": frozenset(),
        "deps": [],
        "write_paths": frozenset(),
        "authorities": frozenset(),
    }

    # Read checkpoint.json — the supervisor already writes/reads this.
    checkpoint_path = state / "checkpoint.json"
    if checkpoint_path.is_file():
        try:
            cp = json.loads(checkpoint_path.read_text())
        except (ValueError, OSError):
            cp = {}
        # Story identity from checkpoint issue field.
        if cp.get("issue"):
            meta["story_id"] = str(cp["issue"])
        # Write paths from OpenSpec change (written by workers into checkpoint).
        if isinstance(cp.get("write_paths"), list):
            meta["write_paths"] = frozenset(str(p) for p in cp["write_paths"] if p)
        # Authorities from checkpoint.
        if isinstance(cp.get("authorities"), list):
            meta["authorities"] = frozenset(str(a) for a in cp["authorities"] if a)
        # Labels from checkpoint (Linear labels forwarded by dispatch).
        if isinstance(cp.get("labels"), list):
            meta["labels"] = frozenset(str(lb) for lb in cp["labels"] if lb)
        # Dependencies with MAIN_VERIFIED evidence.
        if isinstance(cp.get("deps"), list):
            from verdict.orchestration.ready_gate import DepEvidence

            dep_list = []
            for d in cp["deps"]:
                if isinstance(d, dict) and d.get("identifier"):
                    dep_list.append(
                        DepEvidence(
                            identifier=str(d["identifier"]),
                            linear_done=d.get("linear_done"),
                            merge_commit_on_main=d.get("merge_commit_on_main"),
                            verification_record_present=d.get("verification_record_present"),
                        )
                    )
            meta["deps"] = dep_list

    return meta


def _check_admission(repo: Path, state: Path) -> tuple[dict[str, Any] | None, Any]:
    """Run the BOD-157 admission pipeline when the multi-story flag is on.

    Returns ``(None, None)`` when the flag is off (no-op — single-story flock).
    Returns ``(decision_dict, story_lock_handle_or_None)`` when the flag is on.
    The caller MUST hold the story_lock_handle for the duration of the story
    and release it when the story completes.
    """
    if not _multi_story_enabled():
        return None, None
    # Import lazily so flag-off has zero import cost.
    from verdict.orchestration.story_footprint import StoryFootprintV1
    from verdict.orchestration.supervisor_admission import (
        AdmissionState,
        SupervisorGovernorConfig,
        acquire_story_lock,
        evaluate_admission,
        write_story_metadata,
    )

    # If an external evaluator is injected (tests), delegate to it.
    if ADMISSION_EVALUATOR is not None:
        return ADMISSION_EVALUATOR(repo=repo, state=state), None

    # Read real story metadata from existing supervisor state files.
    meta = _read_story_metadata(state)
    story_id: str = meta["story_id"]
    labels: frozenset[str] = meta["labels"]
    deps: list[Any] = meta["deps"]
    write_paths: frozenset[str] = meta["write_paths"]
    authorities: frozenset[str] = meta["authorities"]

    # Cross-process admission state: reads running set from file locks.
    config = SupervisorGovernorConfig()
    admission_state = AdmissionState(state_dir=state)

    main_sha = (
        subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout=10)
        .decode()
        .strip()
    )

    footprint = StoryFootprintV1(
        story_id=story_id, write_paths=write_paths, authorities=authorities
    )

    result = evaluate_admission(
        story_id=story_id,
        labels=labels,
        deps=deps,
        main_sha=main_sha,
        footprint=footprint,
        admission_state=admission_state,
        config=config,
    )

    if not result.admit:
        return result.to_dict(), None

    # Acquire per-story file lock (non-blocking).  If another process
    # already holds this story, the admission was stale — reject.
    handle = acquire_story_lock(state, story_id)
    if handle is None:
        return {
            "admit": False,
            "reason_code": "LOCK_HELD",
            "reason": f"story {story_id!r} lock already held by another process",
        }, None

    # Write footprint metadata so other processes can read it for collision.
    write_story_metadata(handle, footprint, labels)

    return result.to_dict(), handle


# Global admission state removed — cross-process state now lives in file locks.


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")
    temp.replace(path)


@contextlib.contextmanager
def acquire_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(
                "another supervisor owns this repository; do not steal its lock"
            ) from exc
        yield


def stall_reason(
    *, now: float, started: float, progress: float, idle_seconds: float, timeout: float
) -> str | None:
    if now - started >= timeout:
        return "DEADLINE"
    if now - progress >= idle_seconds:
        return "NO_PROGRESS"
    return None


def stop_group(process: subprocess.Popen[Any]) -> None:
    """Only signal the process group this invocation created, then reap it."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    # A exited parent can leave descendants in its original group.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def one_shot_prime_settings() -> dict[str, Any]:
    """Disable Prime-side semantic retry, wait, and backup behavior."""
    settings: dict[str, Any] = load_prime_settings().one_shot_prime_settings()
    return settings


def run_attempt(
    command: list[str],
    cwd: Path,
    log: Path,
    fingerprint: Callable[[], Any],
    idle_seconds: float,
    timeout: float,
    poll: float = 5,
    progress_made: Callable[[Any, Any], bool] | None = None,
    on_started: Callable[[subprocess.Popen[Any]], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    made_progress = progress_made or (lambda old, new: old != new)
    settings_contract = load_prime_settings()
    # Only Verdict-owned launches are one-shot. The policy lives in this
    # per-launch agent dir, not in the tracked project file, so manual sessions
    # in this checkout keep Prime's own retries.
    #
    # PRIME_AGENT_CODING_AGENT_DIR replaces the WHOLE agent dir, so the launch
    # dir mirrors the operator's auth.json, models.json, skills and extensions.
    # An empty dir would launch the controller without credentials or registry.
    child_env = dict(os.environ if env is None else env)
    config_dir = Path(tempfile.mkdtemp(prefix="verdict-controller-prime-"))
    settings_contract.prepare_launch_agent_dir(
        config_dir, source=settings_contract.default_prime_agent_dir(child_env)
    )
    child_env[settings_contract.PRIME_AGENT_DIR_ENV] = str(config_dir)
    # Project settings win over this per-launch config dir in Prime, and Prime
    # 0.9.6 has no per-launch escape, so gate on the EFFECTIVE merged settings.
    # Fail closed: a hidden Prime retry would spend quota and hide a terminal.
    try:
        settings_contract.assert_one_shot_launch(cwd=cwd, config_dir=config_dir)
    except BaseException:
        import shutil

        shutil.rmtree(config_dir, ignore_errors=True)
        raise
    try:
        with log.open("w") as output:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=child_env,
            )
    except BaseException:
        import shutil

        shutil.rmtree(config_dir, ignore_errors=True)
        raise
    try:
        # Startup identity is an admission gate. Do not fingerprint/watchdog,
        # and therefore do not admit the mission, until it succeeds.
        if on_started is not None:
            on_started(process)
        started = progress = time.monotonic()
        previous = fingerprint()
        while process.poll() is None:
            current = fingerprint()
            if made_progress(previous, current):
                previous, progress = current, time.monotonic()
            else:
                previous = current
            reason = stall_reason(
                now=time.monotonic(),
                started=started,
                progress=progress,
                idle_seconds=idle_seconds,
                timeout=timeout,
            )
            if reason:
                stop_group(process)
                return {"reason": reason, "returncode": process.returncode}
            time.sleep(poll)
        return {"reason": "EXIT", "returncode": process.returncode}
    finally:
        stop_group(process)
        import shutil

        shutil.rmtree(config_dir, ignore_errors=True)


_STATUS_RE_PATTERNS = (
    r"\[(\d{3})\]",
    r"\b(?:HTTP|status)[ :=]*(\d{3})\b",
    r"(?<!\d)(4\d\d|5\d\d)(?!\d)",
)


def _log_tail(path: Path, limit: int = 8192) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def ladder_state_path(env: Mapping[str, str] | None = None) -> Path:
    """The ladder state that canonical live admission reads for cooldowns."""
    source = os.environ if env is None else env
    return (
        Path(source.get("VERDICT_HOME") or Path.home() / ".verdict") / "orchestration-health.json"
    )


def classify_generation_failure(
    result: Mapping[str, Any], log: Path, *, route_id: str, now: datetime
) -> dict[str, Any]:
    """Classify a dead controller generation with the shared failure policy.

    The generation's terminal evidence is the tail of its own log. The same
    ``FailureIntelligence`` classifier used for workers decides category,
    scope and cooldown, so root and worker failover agree on semantics.
    """
    import re

    from verdict.orchestration.contracts import WorkerTerminal
    from verdict.orchestration.recovery import FailureIntelligence

    tail = _log_tail(log)
    status: int | None = None
    for pattern in _STATUS_RE_PATTERNS:
        matches = re.findall(pattern, tail)
        codes = [int(m) for m in matches if 400 <= int(m) <= 599]
        if codes:
            status = codes[-1]
            break
    reason = str(result.get("reason") or "")
    stop_reason = "timeout" if reason in {"TIMEOUT", "NO_PROGRESS"} else "error"
    terminal = WorkerTerminal(
        ok=False,
        model=route_id,
        stop_reason=stop_reason,
        error=tail[-2000:] or reason,
        status_code=status,
    )
    failure = FailureIntelligence().classify(terminal, now=now)
    return {
        "route_id": route_id,
        "provider": route_id.split("/", 1)[0].lower() if "/" in route_id else route_id.lower(),
        "status_code": status,
        "result_reason": reason,
        "category": failure.category,
        "scope": failure.scope,
        "action": failure.action,
        "cooldown_seconds": float(failure.cooldown_seconds),
        "evidence": failure.evidence,
        "observed_at": now.isoformat(timespec="seconds"),
    }


def persist_generation_cooldown(
    classification: Mapping[str, Any], *, ladder: Path, now: datetime
) -> dict[str, Any] | None:
    """Write route/provider cooldowns BEFORE the next controller selection.

    Uses the ladder-state format that ``admission.evidence_from_ladder_state``
    reads, so the next generation's canonical admission cannot re-admit a
    cooled route/provider. Request-scoped failures (scope ``none``, e.g. a
    context-length 400) write nothing: the route itself is healthy.
    """
    from datetime import timedelta

    scope = str(classification.get("scope") or "none")
    seconds = float(classification.get("cooldown_seconds") or 0)
    if scope not in {"route", "provider"} or seconds <= 0:
        return None
    until = (now + timedelta(seconds=min(seconds, 7 * 86_400))).isoformat(timespec="seconds")
    entry = {
        "until": until,
        "category": str(classification.get("category") or "cooldown"),
        "source": "prime_supervisor:generation_failure",
    }
    route = str(classification["route_id"])
    try:
        raw = json.loads(ladder.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    state = raw if isinstance(raw, dict) else {}
    existing = state.get("cooldowns")
    cooldowns: dict[str, Any] = existing if isinstance(existing, dict) else {}
    state["cooldowns"] = cooldowns
    state.setdefault("health", {})
    keys = [f"route:{route}"]
    if scope == "provider":
        keys.append(f"provider:{classification['provider']}")
    for key in keys:
        cooldowns[key] = dict(entry)
    ladder.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(ladder, state)
    return {"keys": keys, **entry}


def record_generation_failure(
    *,
    state: Path,
    token: str,
    route_id: str,
    result: Mapping[str, Any],
    log: Path,
    ladder: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify, persist cooldown, and write a generation-failure receipt."""
    when = now or datetime.now(timezone.utc)
    canonical = route_id.removeprefix("omniroute/")
    classification = classify_generation_failure(result, log, route_id=canonical, now=when)
    cooldown = persist_generation_cooldown(
        classification, ladder=ladder or ladder_state_path(), now=when
    )
    receipt = {"generation": token, **classification, "cooldown": cooldown}
    atomic_json(state / f"generation-failure-{token}.json", receipt)
    return receipt


def recover(attempt: Callable[[], dict[str, Any]], state: Path, max_restarts: int) -> int:
    for index in range(max_restarts + 1):
        result = attempt()
        success = result["reason"] == "EXIT" and result["returncode"] == 0
        status = "STOPPED" if success else "RECOVERING" if index < max_restarts else "BLOCKED"
        atomic_json(
            state / "supervisor.json",
            {"status": status, "attempts": index + 1, "result": result, "observed_at": time.time()},
        )
        print(json.dumps({"status": status, **result}), flush=True)
        if success:
            return 0
    return 2


def owned_sessions(roster: dict[str, Any], session_dir: Path) -> list[dict[str, Any]]:
    """Ownership comes from this attempt's unique session directory, never a guessed PID."""
    root = session_dir.resolve()
    if not isinstance(roster, dict) or not isinstance(roster.get("sessions"), list):
        raise ValueError("malformed Prime roster; cannot confirm writer stopped")
    for session in roster["sessions"]:
        if not isinstance(session, dict) or session.get("lifecycle") not in {"live", "saved"}:
            raise ValueError("unknown Prime lifecycle; cannot confirm writer stopped")
        if session["lifecycle"] == "live" and (
            not session.get("sessionFile") or not session.get("id")
        ):
            raise ValueError("live Prime session lacks identity; cannot confirm writer stopped")
    return [
        s
        for s in roster["sessions"]
        if s.get("sessionFile")
        and Path(s["sessionFile"]).resolve().is_relative_to(root)
        and s.get("lifecycle") == "live"
    ]


def stop_owned_daemon(prime: str, session_dir: Path) -> None:
    def roster() -> dict[str, Any]:
        result = json.loads(subprocess.check_output([prime, "list", "--json"], timeout=20))
        if not isinstance(result, dict):
            raise ValueError("malformed Prime roster")
        return result

    # Print mode is daemon-backed; killing its client does not synchronously stop the writer.
    for session in owned_sessions(roster(), session_dir):
        subprocess.run(
            [prime, "stop", session["id"]],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        if not owned_sessions(roster(), session_dir):
            return
        time.sleep(1)
    raise ValueError("owned daemon worker still live; redispatch prohibited")


def git(repo: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(repo), *args], timeout=20)


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def _registered_artifacts(issue_dir: Path) -> list[Path]:
    """Only artifacts a receipt/proof actually references can count as progress."""
    registered: set[str] = set()
    for name in ("receipt.json", "proof.json"):
        path = issue_dir / name
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            continue
        for command in data.get("commands", []) or []:
            if isinstance(command, dict) and isinstance(command.get("artifact"), str):
                registered.add(command["artifact"])
        gates = data.get("gates", {})
        if isinstance(gates, dict):
            for gate in gates.values():
                if isinstance(gate, dict) and isinstance(gate.get("artifact"), str):
                    registered.add(gate["artifact"])
    artifacts = []
    for relative in sorted(registered):
        candidate = (issue_dir / relative).resolve()
        if (
            candidate.is_relative_to(issue_dir.resolve())
            and candidate.is_file()
            and not candidate.is_symlink()
        ):
            artifacts.append(candidate)
    return artifacts


def workspace_fingerprint(repo: Path, state: Path) -> tuple[str, str, str]:
    """Return (substantive, artifact, receipt_identity) progress components.

    Substantive progress is real source/state change. Artifact churn alone is not
    progress, so an alive-but-hung worker cannot keep resetting its own deadline by
    writing log files.
    """
    substantive = hashlib.sha256()
    artifact_digest = hashlib.sha256()
    receipt_identity = hashlib.sha256()
    state_resolved = state.resolve()

    roots = {repo.resolve()}
    checkpoint_path = state / "checkpoint.json"
    checkpoint: dict[str, Any] = {}
    if checkpoint_path.is_file():
        try:
            checkpoint = json.loads(checkpoint_path.read_text())
        except (ValueError, OSError):
            checkpoint = {}
        candidate = str(checkpoint.get("worktree") or "")
        if candidate:
            registered = git(repo, "worktree", "list", "--porcelain").decode().splitlines()
            if f"worktree {candidate}" in registered:
                roots.add(Path(candidate).resolve())
        substantive.update(
            str(
                (
                    checkpoint.get("issue"),
                    checkpoint.get("state"),
                    checkpoint.get("head_sha"),
                    checkpoint.get("proof_digest"),
                    checkpoint.get("verified_state"),
                )
            ).encode()
        )

    for root in sorted(roots):
        substantive.update(git(root, "rev-parse", "HEAD"))
        substantive.update(git(root, "diff", "HEAD", "--", "."))
        names = git(root, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        for raw in sorted(filter(None, names)):
            raw_path = root / os.fsdecode(raw)
            if raw_path.is_relative_to(state_resolved) or raw_path.is_symlink():
                continue
            path = raw_path.resolve()
            if not path.is_file() or path.suffix.lower() not in {
                ".py",
                ".pyi",
                ".js",
                ".jsx",
                ".ts",
                ".tsx",
                ".json",
                ".toml",
                ".yaml",
                ".yml",
            }:
                continue
            substantive.update(raw)
            substantive.update(_digest_file(path).encode())

    leases_root = state / "leases"
    if leases_root.is_dir():
        for active in sorted(leases_root.glob("*/active.json")):
            try:
                lease = json.loads(active.read_text())
            except (ValueError, OSError):
                continue
            substantive.update(
                str(
                    (
                        lease.get("issue"),
                        lease.get("generation"),
                        lease.get("status"),
                        lease.get("last_progress_at"),
                    )
                ).encode()
            )

    issues_root = state / "issues"
    if issues_root.is_dir():
        for issue_dir in sorted(p for p in issues_root.iterdir() if p.is_dir()):
            receipt_path = issue_dir / "receipt.json"
            if receipt_path.is_file():
                try:
                    receipt = json.loads(receipt_path.read_text())
                except (ValueError, OSError):
                    receipt = {}
                receipt_identity.update(
                    str(
                        (
                            issue_dir.name,
                            receipt.get("dispatch_id"),
                            receipt.get("status"),
                            receipt.get("current_step"),
                            receipt.get("last_progress_at"),
                        )
                    ).encode()
                )
            for artifact in _registered_artifacts(issue_dir):
                artifact_digest.update(str(artifact).encode())
                artifact_digest.update(_digest_file(artifact).encode())

    return substantive.hexdigest(), artifact_digest.hexdigest(), receipt_identity.hexdigest()


def progress_made(previous: tuple[str, str, str], current: tuple[str, str, str]) -> bool:
    return bool(STATE.is_progress(*previous, *current))


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ValueError(f"cannot read controller decision from {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"controller decision at {path} must be a JSON object")
    return data


def _prime_target_from_dict(raw: Any) -> PrimeLaunchTarget:
    if not isinstance(raw, dict):
        raise ValueError("prime_target must be an object")
    return PrimeLaunchTarget(
        upstream_provider=str(raw["upstream_provider"]),
        upstream_model=str(raw["upstream_model"]),
        prime_provider=str(raw["prime_provider"]),
        prime_model=str(raw["prime_model"]),
        binding_digest=str(raw["binding_digest"]),
        reasoning_effort=None
        if raw.get("reasoning_effort") in (None, "")
        else str(raw["reasoning_effort"]),
        binding_evidence_ref=None
        if raw.get("binding_evidence_ref") in (None, "")
        else str(raw["binding_evidence_ref"]),
    )


def load_persisted_authoritative_decision(path: Path) -> PersistedAuthoritativeDecision:
    """Load already-authoritative BOD-104 decision + pre-launch receipt reference."""
    data = _load_json(path)
    evidence = data.get("evidence_refs") or []
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("persisted decision requires non-empty evidence_refs")
    freshness = data.get("constituent_freshness") or {}
    if not isinstance(freshness, dict):
        raise ValueError("constituent_freshness must be an object")
    return PersistedAuthoritativeDecision(
        execution_path_decision_digest=str(data["execution_path_decision_digest"]),
        selected_upstream_route=str(data["selected_upstream_route"]),
        prime_target=_prime_target_from_dict(data["prime_target"]),
        context_plan_digest=str(data["context_plan_digest"]),
        context_pack_digest=str(data["context_pack_digest"]),
        context_receipt_digest=str(data["context_receipt_digest"]),
        selected_prompt_digest=str(data["selected_prompt_digest"]),
        routing_receipt_ref=str(data["routing_receipt_ref"]),
        pool_ref=str(data["pool_ref"]),
        evidence_refs=tuple(str(x) for x in evidence),
        constituent_freshness={str(k): str(v) for k, v in freshness.items()},
        minimum_expiry=str(data["minimum_expiry"]),
        session_decision=str(data["session_decision"]),
        why_selected=str(data["why_selected"]),
        task_profile_digest=str(data["task_profile_digest"]),
        task_slice_digest=str(data["task_slice_digest"]),
        trajectory_digest=str(data["trajectory_digest"]),
    )


def load_controller_mission(path: Path | None, *, attempt_id: str) -> ControllerMission:
    if path is None:
        return ControllerMission(
            mission_id="supervisor",
            story_id="supervisor",
            attempt_id=attempt_id,
            objective="Prime controller supervision",
        )
    data = _load_json(path)
    return ControllerMission(
        mission_id=str(data.get("mission_id") or "supervisor"),
        story_id=str(data.get("story_id") or "supervisor"),
        attempt_id=str(data.get("attempt_id") or attempt_id),
        objective=str(data.get("objective") or "Prime controller supervision"),
        spend_ceiling_usd=data.get("spend_ceiling_usd"),
        security_floor=None
        if data.get("security_floor") in (None, "")
        else str(data["security_floor"]),
        required_tools=tuple(data.get("required_tools") or ()),
        required_mcp=tuple(data.get("required_mcp") or ()),
        orchestration_burden=None
        if data.get("orchestration_burden") in (None, "")
        else str(data["orchestration_burden"]),
        context_burden=None
        if data.get("context_burden") in (None, "")
        else str(data["context_burden"]),
        proof_burden=None if data.get("proof_burden") in (None, "") else str(data["proof_burden"]),
        durable_context_refs=tuple(str(x) for x in (data.get("durable_context_refs") or ())),
        task_profile_digest=None
        if data.get("task_profile_digest") in (None, "")
        else str(data["task_profile_digest"]),
        task_slice_digest=None
        if data.get("task_slice_digest") in (None, "")
        else str(data["task_slice_digest"]),
        trajectory_digest=None
        if data.get("trajectory_digest") in (None, "")
        else str(data["trajectory_digest"]),
    )


def _prompt_sha256_digest(prompt: str) -> str:
    """Return sha256:<hex> for exact compiled-prompt digest comparison."""
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _supervisor_instruction_unit(
    *, token: str, state_dir: Path, session_dir: Path, max_issues: int, timeout: float
) -> Any:
    """ContextUnit carrying supervisor run instructions (compiled into prompt)."""
    from verdict.context_pack import ContextUnit

    content = (
        f"/skill:verdict-resume\nSupervisor run_id={token}; state_dir={state_dir}. "
        f"Budget {max_issues} issues across this supervisor run, {timeout}s per session. "
        "Read checkpoint.json and supervisor.json before any action. Reconcile authorities. "
        "This is a fresh context recovery, not permission to repeat completed work. "
        f"Use synchronous child subprocesses with --session-dir {session_dir}; do not detach RLM writers. "
        "Before exit atomically write outcome.json with this run_id, status DONE/IDLE/BLOCKED, "
        "and reason. Preserve progress in checkpoint.json before compacting or returning."
    )
    observed = datetime.now(timezone.utc).isoformat()
    return ContextUnit(
        unit_id=f"supervisor:{token}",
        slot_type="instructions",
        key="supervisor.run_instructions",
        content=content,
        source_uri=f"supervisor://{token}",
        source_digest="sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest(),
        observed_at=observed,
        trust="verified",
        authority="prime_supervisor",
        status="active",
    )


def _bootstrap_failure_detail(exc: Any) -> str:
    """Render a bootstrap refusal as one deterministic supervisor detail line.

    Keeps the configuration / gateway-health / model-eligibility class explicit so
    startup diagnostics never collapse the three into one opaque message.
    """
    parts = [f"{exc.reason_code} [{exc.diagnostic_class}]"]
    parts.extend(diagnostic.describe() for diagnostic in exc.diagnostics)
    return "; ".join(parts)


def _load_prime_target_map_from_config(repo: Path) -> dict[str, Any]:
    """Load trusted PrimeLaunchTarget map from explicit persisted config only.

    Looks for ``.verdict/prime-target-map.json`` under the repo, then
    ``~/.verdict/prime-target-map.json``. Never invents identity-copy bindings.
    """
    candidates = [
        repo / ".verdict" / "prime-target-map.json",
        Path.home() / ".verdict" / "prime-target-map.json",
    ]
    env_path = os.environ.get("VERDICT_PRIME_TARGET_MAP")
    if env_path and str(env_path).strip():
        candidates.insert(0, Path(env_path).expanduser())
    path: Path | None = None
    for candidate in candidates:
        if candidate.is_file():
            path = candidate
            break
    if path is None:
        raise ControllerLaunchError(
            "production_factory_unavailable",
            "no trusted prime-target-map.json found under repo/.verdict, "
            "~/.verdict, or VERDICT_PRIME_TARGET_MAP",
        )
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ControllerLaunchError(
            "production_factory_unavailable", f"cannot read prime target map from {path}: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ControllerLaunchError(
            "production_factory_unavailable", f"prime target map at {path} must be a JSON object"
        )
    entries = raw.get("targets") if "targets" in raw else raw
    if not isinstance(entries, dict) or not entries:
        raise ControllerLaunchError(
            "production_factory_unavailable", f"prime target map at {path} has no target entries"
        )
    out: dict[str, Any] = {}
    for route_id, value in entries.items():
        if not isinstance(value, dict):
            raise ControllerLaunchError(
                "production_factory_unavailable",
                f"prime target map entry {route_id!r} must be an object",
            )
        try:
            out[str(route_id)] = _prime_target_from_dict(value)
        except (KeyError, TypeError, ValueError, ControllerLaunchError) as exc:
            raise ControllerLaunchError(
                "production_factory_unavailable",
                f"invalid PrimeLaunchTarget for {route_id!r}: {exc}",
            ) from exc
    return out


def _build_intelligence_service_from_config(*, repo: Path, state_dir: Path) -> Any:
    """Construct IntelligenceService from the shared bootstrap contract.

    Production requires an authoritative provider set: no built-in local
    default, no silent substitution of an unintended provider, and no use of the
    controller model. A refusal names the exact missing field, its source and the
    operator remediation.

    Credential names are read from the local credential store through the
    contract's ``credential_store_env`` seam, so a key held only in the store is
    reported as ``source="credential_store"`` instead of being refused as
    missing. Store values are passed as an argument, never exported into
    ``os.environ``.
    """
    from verdict.intelligence import IntelligenceService
    from verdict.provider_bootstrap import (
        BootstrapError,
        load_credential_store_env,
        resolve_provider_bootstrap,
    )

    try:
        bootstrap = resolve_provider_bootstrap(
            require_authoritative=True, credential_store_env=load_credential_store_env()
        )
    except BootstrapError as exc:
        raise ControllerLaunchError(
            "production_factory_unavailable", _bootstrap_failure_detail(exc)
        ) from exc

    passport_store = Path.home() / ".verdict" / "prove-at-rest" / "state.json"
    metadata_store = Path.home() / ".verdict" / "model-metadata.json"
    return IntelligenceService(
        primary_model=bootstrap.primary_model,
        providers=bootstrap.provider_configs(),
        profile=bootstrap.profile,
        log_path=bootstrap.log_path,
        log_full_task=False,
        discovery_ttl=int(os.environ.get("LLMGATE_DISCOVERY_TTL_SECONDS", "60")),
        timeout_ms=int(os.environ.get("LLMGATE_INTELLIGENCE_TIMEOUT_MS", "1000")),
        execute_offload=False,
        workspace_root=str(repo),
        passport_store_path=passport_store if passport_store.is_file() else None,
        metadata_store_path=metadata_store if metadata_store.is_file() else None,
        receipt_store=None,
        persist_routing_receipts=True,
        require_execution_path_authority=True,
    )


def _wrap_context_units_with_supervisor(
    base_factory: Callable[..., Any] | None,
    *,
    token: str,
    state_dir: Path,
    session_dir: Path,
    max_issues: int,
    timeout: float,
) -> Callable[..., Any]:
    """Prepend supervisor instruction ContextUnit before compile."""

    def factory(mission: Any, decision: Any, prepared: Any, plan: Any) -> tuple[Any, ...]:
        units: list[Any] = [
            _supervisor_instruction_unit(
                token=token,
                state_dir=state_dir,
                session_dir=session_dir,
                max_issues=max_issues,
                timeout=timeout,
            )
        ]
        if base_factory is not None:
            units.extend(list(base_factory(mission, decision, prepared, plan)))
        else:
            units.extend(
                list(_cs()._default_controller_context_units(mission, decision, prepared, plan))
            )
        return tuple(units)

    return factory


def _mission_required_capability_tier(mission: Any) -> int:
    """Derive BOD-119 required_capability_tier from mission burden fields only."""
    burdens = (
        str(getattr(mission, "orchestration_burden", None) or "").strip().lower(),
        str(getattr(mission, "context_burden", None) or "").strip().lower(),
        str(getattr(mission, "proof_burden", None) or "").strip().lower(),
    )
    if any(b in {"frontier", "critical", "high"} for b in burdens):
        return 3
    if any(b in {"medium", "standard", "elevated"} for b in burdens):
        return 2
    if any(b in {"low", "cheap", "minimal"} for b in burdens):
        return 1
    # Explicit tools/MCP requirements imply at least a standard controller tier.
    if tuple(getattr(mission, "required_tools", ()) or ()) or tuple(
        getattr(mission, "required_mcp", ()) or ()
    ):
        return 2
    return 1


def _expected_cost_for_route(offers: Any, route: Any, *, role: str, when: datetime) -> Any:
    """Reuse an evidence-backed ExpectedStrategyCost for a concrete route.

    Prefer an exact route_id match among seed offers. Fall back to provider/model
    match. Never invent zero USD prices when evidence is absent.
    """
    from dataclasses import replace

    from verdict.expected_cost import ExpectedStrategyCost

    route_id = str(getattr(route, "route_id", "") or "")
    provider = str(getattr(route, "provider", "") or "")
    model = str(getattr(route, "model", "") or "")
    matched = None
    for offer in offers or ():
        offer_route = getattr(offer, "route", None)
        if offer_route is None:
            continue
        if str(getattr(offer_route, "route_id", "") or "") == route_id:
            matched = offer
            break
    if matched is None:
        for offer in offers or ():
            offer_route = getattr(offer, "route", None)
            if offer_route is None:
                continue
            if (
                str(getattr(offer_route, "provider", "") or "") == provider
                and str(getattr(offer_route, "model", "") or "") == model
            ):
                matched = offer
                break
    if matched is None:
        raise ControllerLaunchError(
            "missing_session_cost_evidence",
            f"no evidence-backed expected_cost for {role} route {route_id!r} "
            f"({provider}/{model}); refusing to invent session economics",
        )
    expected = getattr(matched, "expected_cost", None)
    if expected is None or not isinstance(expected, ExpectedStrategyCost):
        raise ControllerLaunchError(
            "missing_session_cost_evidence",
            f"offer for {role} route {route_id!r} lacks ExpectedStrategyCost evidence",
        )
    # Cash may be None (unknown) — that is honest evidence. Fabricating 0.0 is not.
    strategy_id = f"{role}:{route_id or provider + '/' + model}"
    return replace(expected, strategy_id=strategy_id)


def _build_bod119_session_factories(
    *, seed_offers: Callable[..., Any] | None
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any] | None]:
    """Build BOD-119 cost/task factories from live seed-offer evidence.

    Cost factory derives STAY/SWITCH ExpectedStrategyCost from the same
    evidence-backed offers the selector seeds. Task factory derives
    required_capability_tier from ControllerMission burden fields.

    Returns ``(cost_state_factory, task_state_factory, wrapped_seed_offers|None)``.
    When ``seed_offers`` is provided, the wrapped seed fills a shared cache the
    cost factory reads. When None, the caller must wrap hooks.seed_offers after
    the selection factory builds its own evidence-backed seed path.
    """
    from verdict.session_economics import CostState, TaskState

    offer_cache: dict[str, Any] = {"offers": None, "mission": None}

    def _ensure_offers(mission: Any, when: datetime) -> Any:
        cached = offer_cache["offers"]
        if cached is not None and offer_cache["mission"] is mission:
            return cached
        if seed_offers is None:
            raise ControllerLaunchError(
                "missing_session_cost_evidence",
                "session cost factory requires seed_offers evidence; none configured",
            )
        offers = tuple(seed_offers(mission, when))
        if not offers:
            raise ControllerLaunchError(
                "missing_session_cost_evidence",
                "seed_offers returned no evidence-backed offers for session economics",
            )
        offer_cache["offers"] = offers
        offer_cache["mission"] = mission
        return offers

    def cost_state_factory(current: Any, fresh: Any, when: datetime) -> Any:
        mission = offer_cache["mission"]
        if mission is None:
            # Selector calls cost_state_factory after seed_offers(mission, when).
            # If a caller invokes the factory first, fail closed with a named code.
            raise ControllerLaunchError(
                "missing_session_cost_evidence",
                "cost_state_factory invoked before seed_offers captured mission evidence",
            )
        offers = _ensure_offers(mission, when)
        stay = _expected_cost_for_route(offers, current, role="stay", when=when)
        switch = _expected_cost_for_route(offers, fresh, role="switch", when=when)
        return CostState(stay_expected=stay, switch_expected=switch, now=when)

    def task_state_factory(mission: Any) -> Any:
        # Capture mission so a later cost_state_factory call can resolve offers
        # against the same mission context even if seed_offers was not yet called
        # through our wrapper (caller-supplied seed_offers path).
        offer_cache["mission"] = mission
        remaining = None
        ceiling = getattr(mission, "spend_ceiling_usd", None)
        if ceiling is not None:
            try:
                # Cheap horizon signal only — never invents USD costs.
                remaining = max(1, int(float(ceiling)))
            except (TypeError, ValueError):
                remaining = None
        action = None
        for burden_name in ("orchestration_burden", "proof_burden", "context_burden"):
            value = getattr(mission, burden_name, None)
            if value not in (None, ""):
                action = str(value)
                break
        return TaskState(
            required_capability_tier=_mission_required_capability_tier(mission),
            remaining_horizon_turns=remaining,
            action_class=action,
        )

    if seed_offers is not None:
        user_seed = seed_offers

        def wrapped_seed_offers(mission: Any, when: datetime) -> Any:
            offers = tuple(user_seed(mission, when))
            offer_cache["offers"] = offers
            offer_cache["mission"] = mission
            return offers

        return cost_state_factory, task_state_factory, wrapped_seed_offers

    return cost_state_factory, task_state_factory, None


def _live_admission_gateway() -> str:
    """Gateway for live admission, resolved by the shared bootstrap contract.

    Uses the same precedence as every other production path (environment
    ``OMNIROUTE_BASE_URL``, then config ``gateway_url``, then the gateway
    provider's ``base_url``). A config-only operator therefore gets the gateway
    they configured instead of a hardcoded loopback default. When the contract
    names no gateway, admission is unavailable and the launch fails closed.
    """
    from verdict.admission import AdmissionUnavailableError
    from verdict.provider_bootstrap import (
        BootstrapError,
        load_credential_store_env,
        resolve_provider_bootstrap,
    )

    try:
        bootstrap = resolve_provider_bootstrap(
            require_authoritative=True, credential_store_env=load_credential_store_env()
        )
    except BootstrapError as exc:
        raise AdmissionUnavailableError(
            "live_admission_gateway_unresolved", _bootstrap_failure_detail(exc)
        ) from exc
    if not bootstrap.gateway_url:
        raise AdmissionUnavailableError(
            "live_admission_gateway_unresolved",
            "the bootstrap contract names no gateway; set OMNIROUTE_BASE_URL or gateway_url",
        )
    return bootstrap.gateway_url.strip()


def _default_live_admission_loader(state_dir: Path) -> Callable[[datetime], Any]:
    """Canonical live admission for controller seeds (read-only OmniRoute GETs)."""

    def load(when: datetime) -> Any:
        from verdict.admission import load_live_admission

        gateway = _live_admission_gateway()
        key = (
            os.environ.get("VERDICT_OMNIROUTE_API_KEY") or os.environ.get("OMNIROUTE_API_KEY") or ""
        ).strip() or None
        admitted = load_live_admission(gateway, now=when, api_key=key)
        admitted.write_receipt(state_dir / "controller-admission-latest.json")
        return admitted

    return load


def build_production_controller_selection_bundle(
    *,
    repo: Path,
    state_dir: Path,
    session_dir: Path | None = None,
    token: str | None = None,
    max_issues: int = 5,
    timeout: float = 3600.0,
    intelligence_service: Any | None = None,
    certify_runtime_fn: Callable[..., Any] | None = None,
    **factory_kwargs: Any,
) -> Any:
    """Build production selection bundle from real persisted inputs/env configs.

    Fail closed with ``production_factory_unavailable`` when IntelligenceService,
    trusted Prime binding, or live evidence cannot be constructed exactly.
    Never invents live snapshots, eligibility, metadata, target binding, or
    prompt evidence.

    Always supplies BOD-119 ``cost_state_factory`` / ``task_state_factory`` derived
    from evidence-backed seed offers and mission requirements (unless the caller
    already injected both). Production must not fail merely because no prior
    SessionState exists — continuity factories are present for when it does.
    """
    global _LAST_PRODUCTION_SELECTION_BUNDLE
    try:
        service = intelligence_service
        if service is None and factory_kwargs.get("prepare_execution_request") is None:
            service = _build_intelligence_service_from_config(repo=repo, state_dir=state_dir)

        kwargs = dict(factory_kwargs)
        # The selection factory defaults to real, offline BOD-92 certification
        # from the loaded passports. Tests may inject a report producer here.
        if certify_runtime_fn is not None:
            kwargs["certify_runtime_fn"] = certify_runtime_fn
        kwargs.setdefault("workspace_root", repo)
        if service is not None:
            kwargs.setdefault("intelligence_service", service)

        if (
            kwargs.get("bind_prime_target") is None
            and kwargs.get("prime_target_map") is None
            and kwargs.get("load_prime_target_map") is None
        ):
            kwargs["load_prime_target_map"] = lambda: _load_prime_target_map_from_config(repo)

        if token is not None and session_dir is not None:
            existing_units = kwargs.get("context_units_for_decision")
            kwargs["context_units_for_decision"] = _wrap_context_units_with_supervisor(
                existing_units,
                token=token,
                state_dir=state_dir,
                session_dir=session_dir,
                max_issues=max_issues,
                timeout=timeout,
            )

        # BOD-119 session continuity factories (M5). Prefer caller injection;
        # otherwise derive from the same seed_offers evidence the selector uses.
        # Replace explicit None (setdefault would keep None and CS would raise
        # missing_session_hooks).
        if kwargs.get("cost_state_factory") is None or kwargs.get("task_state_factory") is None:
            cost_factory, task_factory, wrapped_seed = _build_bod119_session_factories(
                seed_offers=kwargs.get("seed_offers")
            )
            if kwargs.get("cost_state_factory") is None:
                kwargs["cost_state_factory"] = cost_factory
            if kwargs.get("task_state_factory") is None:
                kwargs["task_state_factory"] = task_factory
            if wrapped_seed is not None and kwargs.get("seed_offers") is not None:
                kwargs["seed_offers"] = wrapped_seed

        # Authoritative config-built service: canonical live admission is the
        # sole seed authority before ranking (read-only GETs; fails closed).
        if service is not None and kwargs.get("admission") is None:
            kwargs["admission"] = _default_live_admission_loader(state_dir)

        selection = _cs()
        if hasattr(selection, "build_production_controller_selection_bundle"):
            bundle = selection.build_production_controller_selection_bundle(**kwargs)
        else:
            bundle = selection.build_production_controller_selection_hooks(**kwargs)

        # When the selection factory built its own seed_offers (passport path),
        # wrap the resulting hooks so cost_state_factory can read live offers.
        hooks = getattr(bundle, "hooks", None)
        if (
            hooks is not None
            and kwargs.get("seed_offers") is None
            and factory_kwargs.get("seed_offers") is None
        ):
            from dataclasses import replace as _dc_replace

            offer_cache: dict[str, Any] = {"offers": None, "mission": None}
            inner_seed = hooks.seed_offers
            inner_cost = hooks.cost_state_factory
            inner_task = hooks.task_state_factory

            def capturing_seed(mission: Any, when: datetime) -> Any:
                offers = tuple(inner_seed(mission, when))
                offer_cache["offers"] = offers
                offer_cache["mission"] = mission
                return offers

            def cost_from_captured(current: Any, fresh: Any, when: datetime) -> Any:
                from verdict.session_economics import CostState

                offers = offer_cache["offers"]
                if not offers:
                    # Fall back to original factory (may still fail closed).
                    if inner_cost is None:
                        raise ControllerLaunchError(
                            "missing_session_cost_evidence",
                            "no captured seed offers for session cost derivation",
                        )
                    return inner_cost(current, fresh, when)
                stay = _expected_cost_for_route(offers, current, role="stay", when=when)
                switch = _expected_cost_for_route(offers, fresh, role="switch", when=when)
                return CostState(stay_expected=stay, switch_expected=switch, now=when)

            def task_from_mission(mission: Any) -> Any:
                offer_cache["mission"] = mission
                if inner_task is not None:
                    return inner_task(mission)
                from verdict.session_economics import TaskState

                return TaskState(
                    required_capability_tier=_mission_required_capability_tier(mission)
                )

            bundle = _dc_replace(
                bundle,
                hooks=_dc_replace(
                    hooks,
                    seed_offers=capturing_seed,
                    cost_state_factory=cost_from_captured,
                    task_state_factory=task_from_mission,
                ),
            )
    except ControllerLaunchError as exc:
        if exc.reason_code in {
            "production_factory_unavailable",
            "missing_session_hooks",
            "missing_session_cost_evidence",
        }:
            raise
        raise ControllerLaunchError(
            "production_factory_unavailable", f"{exc.reason_code}: {exc.detail}"
        ) from exc
    except Exception as exc:
        raise ControllerLaunchError(
            "production_factory_unavailable",
            f"failed to construct production controller selection: {exc}",
        ) from exc

    _LAST_PRODUCTION_SELECTION_BUNDLE = bundle
    return bundle


def load_session_state_from_prior(*, state_dir: Path, repo: Path) -> Any | None:
    """Best-effort BOD-119 SessionState from a prior controller decision/receipt.

    Only loads when a prior ``controller-decision-*.json`` under state_dir (or an
    explicit prior decision artifact) carries a concrete selected route. Never
    reads ``state/controller_decision.json`` implicitly as launch authority —
    this is continuity input only. Returns None when unavailable.
    """
    from verdict.session_economics import ConcreteRoute, SessionState

    # Prefer newest controller-decision-*.json written by a previous attempt.
    candidates = sorted(
        state_dir.glob("controller-decision-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        return None
    path = candidates[0]
    try:
        data = json.loads(path.read_text())
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    target = data.get("prime_target")
    route_id = str(data.get("selected_upstream_route") or "").strip()
    if not isinstance(target, dict) or not route_id:
        return None
    provider = str(target.get("upstream_provider") or target.get("prime_provider") or "").strip()
    model = str(target.get("upstream_model") or target.get("prime_model") or "").strip()
    if not provider or not model:
        return None
    # Strip gateway prefix if present in selected_upstream_route.
    concrete_id = route_id
    if route_id.startswith("omniroute/") and "/" in route_id[len("omniroute/") :]:
        concrete_id = route_id
    try:
        route = ConcreteRoute(
            route_id=concrete_id if "/" in concrete_id else f"{provider}/{model}",
            gateway="omniroute",
            provider=provider,
            model=model,
            credential_pool=None,
            capability_tier=2,
            # A persisted decision is continuity input, not live eligibility
            # evidence.  Keep prior state pessimistic until the selector has
            # prepared/admitted current candidates; otherwise BOD-119 can
            # forge STAY and later fail with session_ep_mismatch.
            eligible=False,
            excluded=False,
            exclusion_reason="prior_route_unverified",
        )
    except Exception:
        return None
    session_id = str(data.get("attempt_id") or path.stem)
    try:
        return SessionState(session_id=session_id, current_route=route, last_served_route=route)
    except Exception:
        return None


def persist_controller_lifecycle_outcome(
    *,
    repo: Path,
    decision: ControllerLaunchDecision,
    identity: dict[str, Any] | None,
    result: dict[str, Any],
    outcome_status: str | None = None,
) -> None:
    """Persist BOD-144 observed identity / terminal outcome via real receipt API.

    Uses ``finalize_routing_receipt`` when the decision carries a
    ``receipt://`` ref loadable from the default store. Missing receipt is a
    no-op (selection already persisted in-progress); never fakes a store write.
    """
    ref = decision.routing_receipt_ref
    if not ref or not str(ref).startswith("receipt://"):
        return
    receipt_id = str(ref)[len("receipt://") :]
    if not receipt_id:
        return
    try:
        from verdict.routing_receipt import (
            default_receipt_store,
            finalize_routing_receipt,
            load_routing_receipt,
        )
    except Exception:
        return
    try:
        store = default_receipt_store(repo)
        receipt = load_routing_receipt(store, receipt_id=receipt_id)
        if receipt is None:
            return
        success = (
            result.get("reason") == "EXIT"
            and result.get("returncode") == 0
            and (outcome_status in {None, "DONE", "IDLE"})
        )
        observed = None
        if identity is not None:
            observed = {
                "gateway": "omniroute",
                "provider": str(identity.get("provider") or decision.prime_target.prime_provider),
                "model": str(identity.get("model") or decision.prime_target.prime_model),
                "resource_pool": "default",
                "route_id": decision.selected_upstream_route,
            }
        finalize_routing_receipt(
            store,
            receipt,
            outcome="success" if success else "failed",
            observed_identity=observed,
            execution_status="completed" if success else "failed",
        )
    except Exception:
        # Lifecycle persistence must not invent authority or crash the supervisor.
        return


def resolve_controller_decision(
    *,
    decision_path: Path | None,
    mission_path: Path | None,
    attempt_id: str,
    provider: str | None,
    model: str | None,
    thinking: str | None,
    override_reason: str | None,
    selector: Any | None = None,
    selection_hooks: Any | None = None,
    session_state: Any | None = None,
    now: datetime | None = None,
) -> ControllerLaunchDecision:
    """Resolve an exact controller decision for launch.

    Automatic mode (no ``--provider``/``--model``):
      Prefer an injected selector / selection hooks that generate a decision from
      live eligibility + ContextPlans + BOD-104 (+ optional BOD-119). An explicit
      ``--controller-decision`` file remains supported for tests/operators who
      already persisted an authoritative decision. Missing live eligibility must
      block launch — never fall back to a default model.

    Override mode (both provider and model):
      Build CLI provenance and require the identity to qualify through the same
      selector path when hooks/selector are available; otherwise require a
      persisted eligible target binding.
    """
    when = now or datetime.now(timezone.utc)
    has_provider = bool(provider and str(provider).strip())
    has_model = bool(model and str(model).strip())
    if has_provider ^ has_model:
        raise ControllerLaunchError(
            "incomplete_override",
            "--provider and --model must be supplied together for explicit override",
        )

    mission = load_controller_mission(mission_path, attempt_id=attempt_id)
    if mission.attempt_id != attempt_id:
        mission = ControllerMission(
            mission_id=mission.mission_id,
            story_id=mission.story_id,
            attempt_id=attempt_id,
            objective=mission.objective,
            spend_ceiling_usd=mission.spend_ceiling_usd,
            security_floor=mission.security_floor,
            required_tools=mission.required_tools,
            required_mcp=mission.required_mcp,
            orchestration_burden=mission.orchestration_burden,
            context_burden=mission.context_burden,
            proof_burden=mission.proof_burden,
            durable_context_refs=mission.durable_context_refs,
            task_profile_digest=mission.task_profile_digest,
            task_slice_digest=mission.task_slice_digest,
            trajectory_digest=mission.trajectory_digest,
        )

    override = None
    if has_provider and has_model:
        override = OperatorOverride(
            provider=str(provider),
            model=str(model),
            source="cli",
            reason=(override_reason or "explicit CLI override").strip() or "explicit CLI override",
            timestamp=when.isoformat(),
            reasoning_effort=None if not thinking else str(thinking),
        )
    elif thinking:
        raise ControllerLaunchError(
            "thinking_without_override",
            "--thinking is only valid with explicit --provider/--model override "
            "against a supported target; automatic mode uses the approved decision",
        )

    # 1) Injected selector object (tests / production wiring).
    if selector is not None:
        return cast(
            ControllerLaunchDecision,
            selector.select_controller_launch(
                mission, override=override, session_state=session_state, now=when
            ),
        )

    # 2) Injected selection hooks (generate decision from live authorities).
    if selection_hooks is not None:
        return cast(
            ControllerLaunchDecision,
            select_controller_launch(
                mission,
                hooks=selection_hooks,
                override=override,
                session_state=session_state,
                now=when,
            ),
        )

    # 3) Explicit persisted decision file (test/operator input only).
    if decision_path is not None and decision_path.is_file():
        persisted = load_persisted_authoritative_decision(decision_path)
        return decide_controller_launch(mission, persisted=persisted, override=override, now=when)

    # 4) Automatic mode without selector/hooks/file: fail closed.
    raise ControllerLaunchError(
        "missing_authoritative_decision",
        "automatic controller launch requires a live selector (or selection hooks) "
        "to generate a Verdict decision from eligibility/ContextPlan/BOD-104, "
        "or an explicit persisted --controller-decision file; refusing to launch",
    )


def build_supervisor_prime_command(
    *, prime: str, repo: Path, session_dir: Path, decision: ControllerLaunchDecision, prompt: str
) -> list[str]:
    """Exact approved argv. No auto/*, no hidden fallback, no silent thinking downgrade."""
    target = decision.prime_target
    for label, value in (("provider", target.prime_provider), ("model", target.prime_model)):
        lowered = value.lower()
        if lowered.startswith("auto/") or lowered in {"auto", "default"}:
            raise ControllerLaunchError(
                "forbidden_identity", f"refusing to launch with {label}={value!r}"
            )
    argv = [
        prime,
        "--cwd",
        str(repo),
        "--provider",
        target.prime_provider,
        "--model",
        target.prime_model,
        "--session-dir",
        str(session_dir),
    ]
    if target.reasoning_effort:
        argv.extend(["--thinking", target.reasoning_effort])
    argv.extend(["-p", "--mode", "json", prompt])
    return argv


def verify_startup_controller_identity(
    *,
    prime: str,
    session_dir: Path,
    decision: ControllerLaunchDecision,
    timeout: float = 20.0,
    poll: float = 0.5,
) -> dict[str, Any]:
    """Observe roster and verify exact provider/model/thinking for owned root."""
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            roster = json.loads(subprocess.check_output([prime, "list", "--json"], timeout=20))
            observed = observe_owned_controller_identity(roster, session_dir=session_dir)
            verify_observed_controller_identity(decision, observed, session_dir=session_dir)
            return {
                "session_id": observed.session_id,
                "provider": observed.provider,
                "model": observed.model,
                "thinking_level": observed.thinking_level,
                "session_file": observed.session_file,
            }
        except (
            ControllerLaunchError,
            ValueError,
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
        ) as exc:
            last_error = exc
            if isinstance(exc, ControllerLaunchError) and exc.reason_code in {
                "identity_mismatch",
                "session_boundary_violation",
                "malformed_owned_identity",
                "ambiguous_owned_root",
                "malformed_roster",
            }:
                break
            time.sleep(poll)
    if isinstance(last_error, ControllerLaunchError) and last_error.reason_code in {
        "identity_mismatch",
        "session_boundary_violation",
        "malformed_owned_identity",
        "ambiguous_owned_root",
        "malformed_roster",
    }:
        raise last_error
    detail = str(last_error) if last_error else "owned root not observed"
    raise ControllerLaunchError("controller_identity_unverified", detail)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Durable state directory (default: <git-common-dir>/verdict-prime)",
    )
    parser.add_argument("--prime", default="prime-agent")
    parser.add_argument(
        "--provider",
        default=None,
        help="Explicit override provider (requires --model). Omit both for automatic mode.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Explicit override model (requires --provider). Omit both for automatic mode.",
    )
    parser.add_argument(
        "--thinking",
        default=None,
        help="Optional explicit reasoning effort; only with override against supported target.",
    )
    parser.add_argument(
        "--override-reason",
        default=None,
        help="Provenance reason recorded for explicit CLI override.",
    )
    parser.add_argument(
        "--controller-decision",
        type=Path,
        default=None,
        help=(
            "Optional path to an already-persisted authoritative controller decision. "
            "Automatic mode normally generates the decision via the live Verdict selector; "
            "this flag remains for explicit persisted input/tests."
        ),
    )
    parser.add_argument(
        "--controller-mission",
        type=Path,
        default=None,
        help="Optional ControllerMission JSON; defaults to supervisor mission.",
    )
    parser.add_argument(
        "--identity-timeout",
        type=float,
        default=20.0,
        help="Seconds to wait for owned-root roster identity verification after launch.",
    )
    parser.add_argument(
        "--skip-identity-verify",
        action="store_true",
        help="Test-only escape; production supervisors must verify observed identity.",
    )
    parser.add_argument("--idle-seconds", type=float, default=600)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--poll-seconds", type=float, default=5, help="Watchdog poll interval")
    parser.add_argument("--max-restarts", type=int, default=2)
    parser.add_argument("--max-issues", type=int, default=5)
    args = parser.parse_args()
    if args.skip_identity_verify and os.environ.get("VERDICT_TEST_MODE") != "1":
        parser.error("--skip-identity-verify is only allowed when VERDICT_TEST_MODE=1")
    if (
        args.idle_seconds <= 0
        or args.timeout <= 0
        or args.poll_seconds <= 0
        or not 0 <= args.max_restarts <= 5
        or args.max_issues < 1
    ):
        parser.error("positive budgets required; max-restarts must be 0..5")
    repo = args.repo.resolve()
    if args.state_dir is not None:
        state = args.state_dir.resolve()
    else:
        common = Path(
            git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()
        )
        state = common / "verdict-prime"
    state.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    count = 0

    def interrupted(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt("supervisor interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)

    def attempt() -> dict[str, Any]:
        nonlocal count
        count += 1
        token = f"{run_id}-{count}"
        session_dir = state / "sessions" / token
        session_dir.mkdir(parents=True)
        # Only an explicit --controller-decision may use a persisted decision.
        # Never implicitly load state/controller_decision.json.
        decision_path = (
            args.controller_decision.resolve() if args.controller_decision is not None else None
        )
        production_bundle = None
        selector = CONTROLLER_SELECTOR
        selection_hooks = CONTROLLER_SELECTION_HOOKS
        session_state = CONTROLLER_SESSION_STATE
        # Bare automatic path: when no injected selector/hooks and no explicit
        # decision file, build production selection from real config before resolve.
        needs_production = selector is None and selection_hooks is None and decision_path is None
        try:
            if needs_production:
                factory = (
                    CONTROLLER_SELECTION_FACTORY or build_production_controller_selection_bundle
                )
                production_bundle = factory(
                    repo=repo,
                    state_dir=state,
                    session_dir=session_dir,
                    token=token,
                    max_issues=args.max_issues,
                    timeout=args.timeout,
                )
                selection_hooks = production_bundle.hooks
            if session_state is None and selection_hooks is not None:
                # Optional BOD-119 continuity from prior attempt decision/receipt.
                session_state = load_session_state_from_prior(state_dir=state, repo=repo)
            decision = resolve_controller_decision(
                decision_path=decision_path,
                mission_path=args.controller_mission,
                attempt_id=token,
                provider=args.provider,
                model=args.model,
                thinking=args.thinking,
                override_reason=args.override_reason,
                selector=selector,
                selection_hooks=selection_hooks,
                session_state=session_state,
            )
        except ControllerLaunchError as exc:
            atomic_json(
                state / "supervisor.json",
                {
                    "status": "BLOCKED",
                    "attempts": count,
                    "reason": f"{exc.reason_code}: {exc.detail}",
                    "reason_code": exc.reason_code,
                    "observed_at": time.time(),
                },
            )
            raise ValueError(f"{exc.reason_code}: {exc.detail}") from exc

        atomic_json(
            state / "run.json",
            {
                "run_id": token,
                "max_issues": args.max_issues,
                "started_at": time.time(),
                "state_dir": str(state),
                "controller_mode": decision.mode,
                "controller_digest": decision.canonical_digest,
                "prime_provider": decision.prime_target.prime_provider,
                "prime_model": decision.prime_target.prime_model,
                "routing_receipt_ref": decision.routing_receipt_ref,
                "selected_prompt_digest": decision.selected_prompt_digest,
            },
        )
        atomic_json(state / f"controller-decision-{token}.json", decision.to_dict())

        # Exact argv prompt = compiled prompt whose digest matches the decision.
        try:
            prompt: str
            if production_bundle is not None:
                compiled = getattr(production_bundle.artifacts, "last_compiled", None)
                if (
                    compiled is None
                    or not str(getattr(compiled, "compiled_prompt", "") or "").strip()
                ):
                    raise ControllerLaunchError(
                        "missing_compiled_prompt",
                        "production selection completed without a compiled prompt artifact",
                    )
                prompt = compiled.compiled_prompt
                actual_digest = _prompt_sha256_digest(prompt)
                if actual_digest != decision.selected_prompt_digest:
                    raise ControllerLaunchError(
                        "prompt_digest_mismatch",
                        "compiled prompt sha256 does not match decision.selected_prompt_digest "
                        f"(got {actual_digest}, expected {decision.selected_prompt_digest})",
                    )
                if (
                    compiled.route_id
                    and decision.selected_upstream_route
                    and compiled.route_id != decision.selected_upstream_route
                    and not decision.selected_upstream_route.endswith(compiled.route_id)
                    and compiled.route_id not in decision.selected_upstream_route
                ):
                    raise ControllerLaunchError(
                        "prompt_route_mismatch",
                        f"compiled route {compiled.route_id!r} does not match "
                        f"selected_upstream_route {decision.selected_upstream_route!r}",
                    )
            else:
                # Persisted-decision / injected-selector paths: no compiled artifact.
                # Keep a deterministic supervisor prompt (tests / explicit file mode).
                prompt = (
                    "/skill:verdict-resume\n"
                    f"Supervisor run_id={token}; state_dir={state}. "
                    f"Budget {args.max_issues} issues across this supervisor run, "
                    f"{args.timeout}s per session. "
                    "Read checkpoint.json and supervisor.json before any action. "
                    "Reconcile authorities. "
                    "This is a fresh context recovery, not permission to repeat completed work. "
                    f"Use synchronous child subprocesses with --session-dir {session_dir}; "
                    "do not detach RLM writers. "
                    "Before exit atomically write outcome.json with this run_id, "
                    "status DONE/IDLE/BLOCKED, "
                    "and reason. Preserve progress in checkpoint.json before compacting "
                    "or returning."
                )
            command = build_supervisor_prime_command(
                prime=args.prime,
                repo=repo,
                session_dir=session_dir,
                decision=decision,
                prompt=prompt,
            )
        except ControllerLaunchError as exc:
            atomic_json(
                state / "supervisor.json",
                {
                    "status": "BLOCKED",
                    "attempts": count,
                    "reason": f"{exc.reason_code}: {exc.detail}",
                    "reason_code": exc.reason_code,
                    "observed_at": time.time(),
                },
            )
            raise ValueError(f"{exc.reason_code}: {exc.detail}") from exc
        # Workers launched under this controller hard-exclude its identity.
        child_env = {
            **os.environ,
            "VERDICT_ACTIVE_CONTROLLER_ROUTE": decision.selected_upstream_route,
        }
        last_admission = getattr(
            getattr(production_bundle, "artifacts", None), "last_admission", None
        )
        if last_admission is not None:
            last_admission.write_receipt(state / f"controller-admission-{token}.json")
        identity: dict[str, Any] | None = None

        def admit_started(process: subprocess.Popen[Any]) -> None:
            del process  # The roster is the authoritative startup identity.
            nonlocal identity
            if args.skip_identity_verify:
                return
            identity = verify_startup_controller_identity(
                prime=args.prime,
                session_dir=session_dir,
                decision=decision,
                timeout=args.identity_timeout,
                poll=min(args.poll_seconds, 0.5),
            )
            atomic_json(state / f"observed-identity-{token}.json", identity)

        outcome_status: str | None = None
        try:
            result = run_attempt(
                command,
                repo,
                state / f"session-{token}.log",
                lambda: workspace_fingerprint(repo, state),
                args.idle_seconds,
                args.timeout,
                poll=args.poll_seconds,
                progress_made=progress_made,
                on_started=admit_started,
                env=child_env,
            )
        finally:
            stop_owned_daemon(args.prime, session_dir)
        if not (result["reason"] == "EXIT" and result["returncode"] == 0):
            # Supervisor owns root recovery: classify the dead generation and
            # persist its cooldown before recover() starts the next generation,
            # whose fresh admission then cannot reselect a cooled route/provider.
            failure = record_generation_failure(
                state=state,
                token=token,
                route_id=decision.selected_upstream_route,
                result=result,
                log=state / f"session-{token}.log",
            )
            result = {**result, "failure": failure}
        if result["reason"] == "EXIT" and result["returncode"] == 0:
            try:
                outcome = json.loads((state / "outcome.json").read_text())
            except (ValueError, OSError):
                outcome = {}
            if (
                outcome.get("run_id") != token
                or outcome.get("status") not in {"DONE", "IDLE", "BLOCKED"}
                or not outcome.get("reason")
            ):
                persist_controller_lifecycle_outcome(
                    repo=repo,
                    decision=decision,
                    identity=identity,
                    result={"reason": "MISSING_OUTCOME", "returncode": 1},
                    outcome_status=None,
                )
                return {"reason": "MISSING_OUTCOME", "returncode": 1}
            outcome_status = str(outcome["status"])
            if outcome["status"] == "BLOCKED":
                persist_controller_lifecycle_outcome(
                    repo=repo,
                    decision=decision,
                    identity=identity,
                    result=result,
                    outcome_status=outcome_status,
                )
                # Deliberate block needs an external change, never repeat it automatically.
                raise ValueError(f"Prime blocked: {outcome['reason']}")
        persist_controller_lifecycle_outcome(
            repo=repo,
            decision=decision,
            identity=identity,
            result=result,
            outcome_status=outcome_status,
        )
        return result

    try:
        with acquire_lock(state / "supervisor.lock"):
            try:
                # Fence stale writers first: a four-hour-old lease must not survive a restart.
                reaped = STATE.supersede_stale_leases(state, now=time.time())
                if reaped:
                    print(json.dumps({"reaped_stale_leases": reaped}), flush=True)
                # Recover daemon writers left by a previous hard-killed supervisor before admission.
                sessions_root = state / "sessions"
                for previous in sorted(sessions_root.glob("*")) if sessions_root.is_dir() else []:
                    if previous.is_dir() and not previous.is_symlink():
                        stop_owned_daemon(args.prime, previous)
                # BOD-157: multi-story admission gate (ADR-037).
                # When VERDICT_MULTI_STORY=on, the 3-gate pipeline
                # (ready → collide → governor) must ADMIT before the
                # story starts.  Flag off (default) → single-story flock,
                # identical to origin/main.
                admission_decision, story_lock = _check_admission(repo, state)
                if admission_decision is not None and not admission_decision["admit"]:
                    print(json.dumps({"admission": "rejected", **admission_decision}), flush=True)
                    atomic_json(
                        state / "supervisor.json",
                        {
                            "status": "DEFERRED",
                            "reason": admission_decision["reason"],
                            "reason_code": admission_decision["reason_code"],
                            "observed_at": time.time(),
                        },
                    )
                    return 0
                try:
                    return recover(attempt, state, args.max_restarts)
                finally:
                    if story_lock is not None:
                        story_lock.release()
            except (
                ControllerLaunchError,
                ValueError,
                OSError,
                subprocess.SubprocessError,
                KeyboardInterrupt,
            ) as exc:
                atomic_json(
                    state / "supervisor.json",
                    {
                        "status": "BLOCKED",
                        "attempts": count,
                        "reason": str(exc),
                        **(
                            {"reason_code": exc.reason_code}
                            if isinstance(exc, ControllerLaunchError)
                            else {}
                        ),
                        "observed_at": time.time(),
                    },
                )
                raise
    except (
        ControllerLaunchError,
        ValueError,
        OSError,
        subprocess.SubprocessError,
        KeyboardInterrupt,
    ) as exc:
        print(f"BLOCKED: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
