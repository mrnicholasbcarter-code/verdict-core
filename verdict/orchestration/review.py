"""Independent OpenCodeReview: independent, fail-closed semantic review via OpenCodeReview (``ocr``).

``OpenCodeReviewer`` runs Alibaba's ``open-code-review`` CLI (v1.12.x) as an
independent semantic review gate that implements the :class:`Reviewer` protocol
from :mod:`verdict.orchestration.contracts`.

Independence and fail-closed behaviour are the whole point:

* The reviewer model is selected fresh, with the implementers' routes/families
  excluded, so the review is never done by a model that wrote the code.
* The gate never returns ``PASS`` unless the review really ran and is clean. A
  non-zero exit, timeout, missing/unparseable output, or an "every item failed"
  run all become ``ERROR`` (never ``PASS``).

The user's global ``ocr`` config is never mutated: the CLI stores config under
``$HOME/.opencodereview/config.json``, so each run gets an isolated ``HOME``
inside ``out_dir`` that is populated with a temporary provider config (including
the API key) and deleted afterwards. The API key is read from an environment
variable at runtime and never written into the repo, argv, or logs.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, cast

from verdict.credentials_store import resolve_gateway_key
from verdict.orchestration.contracts import (
    FailureClassification,
    ModelSelector,
    ReviewFinding,
    ReviewResult,
    TaskRequirements,
    dispatch_blocker,
    require_launchable,
)
from verdict.orchestration.controller_operations import (
    OwnershipError,
    executor_timeout_within,
    owned_worker_command,
)
from verdict.orchestration.provider_catalog import aliased_pools_for

__all__ = ["OcrRun", "OcrRunner", "OpenCodeReviewer", "make_subprocess_runner"]

# OCR severity vocabulary -> Verdict's normalized ladder.
_SEVERITY_MAP: Mapping[str, str] = {
    "critical": "critical",
    "blocker": "critical",
    "fatal": "critical",
    "high": "high",
    "error": "high",
    "major": "high",
    "medium": "medium",
    "moderate": "medium",
    "warning": "medium",
    "warn": "medium",
    "low": "low",
    "minor": "low",
    "info": "info",
    "informational": "info",
    "note": "info",
    "notice": "info",
    "suggestion": "info",
}
_VALID_SEVERITY = frozenset({"critical", "high", "medium", "low", "info"})


def _normalize_severity(raw: object) -> str:
    text = str(raw or "").strip().lower()
    return _SEVERITY_MAP.get(text, "info")


@dataclass(frozen=True)
class OcrRun:
    """Terminal result of one ``ocr`` subprocess invocation."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    # BOD-224: killed because it made no progress (no output) for the idle window.
    idle_timed_out: bool = False


class OcrRunner(Protocol):
    """Runs one ``ocr`` argv to a terminal :class:`OcrRun`.

    Implementations must never raise for process/transport failures and must
    enforce ``timeout`` themselves (returning ``timed_out=True`` on expiry).
    ``env`` carries the isolated ``HOME`` and must be applied to the child.
    Some runners also accept an optional ``stop_event`` keyword for cooperative
    cancellation and cleanup before the caller reports a terminal failure.
    """

    def __call__(
        self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float
    ) -> OcrRun: ...


_RUNNER_SETTLE_SECONDS = 2.0


def make_subprocess_runner(idle_seconds: float | None = None) -> OcrRunner:
    """``ocr`` child-process runner with a hard timeout and an optional idle timeout.

    Progress is any new stdout/stderr bytes or growth of the ``--output`` file.
    When nothing progresses for ``idle_seconds`` the whole process group is
    killed and the run is reported as ``idle_timed_out`` (BOD-224), so a hung
    reviewer is replaced long before the hard timeout.
    """

    def run(
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        timeout: float,
        stop_event: threading.Event | None = None,
    ) -> OcrRun:
        return _run_with_watchdog(
            argv, env=env, timeout=timeout, idle_seconds=idle_seconds, stop_event=stop_event
        )

    return run


def _run_with_watchdog(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout: float,
    idle_seconds: float | None,
    stop_event: threading.Event | None = None,
) -> OcrRun:
    import signal

    environment = {**os.environ, **env}
    try:
        argv = owned_worker_command(list(argv), environment)
    except OwnershipError as exc:
        return OcrRun(exit_code=125, stderr=f"owned worker launch refused: {exc}")
    output_path = Path(argv[argv.index("--output") + 1]) if "--output" in argv else None
    try:
        proc = subprocess.Popen(
            argv,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return OcrRun(exit_code=127, stderr=str(exc))

    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    last_activity = [time.monotonic()]

    def pump(name: str, stream: Any) -> None:
        for chunk in iter(lambda: stream.read1(4096), b""):
            chunks[name].append(chunk)
            last_activity[0] = time.monotonic()

    readers = [
        threading.Thread(target=pump, args=(name, stream), daemon=True)
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for reader in readers:
        reader.start()

    started = time.monotonic()
    output_size = -1
    reason = ""
    while proc.poll() is None:
        now = time.monotonic()
        if output_path is not None:
            try:
                size = output_path.stat().st_size
            except OSError:
                size = -1
            if size != output_size:
                output_size = size
                if size >= 0:
                    last_activity[0] = now
        if now - started >= timeout:
            reason = "timeout"
            break
        if idle_seconds is not None and now - last_activity[0] >= idle_seconds:
            reason = "idle"
            break
        if stop_event is not None and stop_event.is_set():
            reason = "cancelled"
            break
        time.sleep(0.05)
    if reason:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    proc.wait()
    for reader in readers:
        reader.join(timeout=2)
    stdout = b"".join(chunks["stdout"]).decode("utf-8", "replace")
    stderr = b"".join(chunks["stderr"]).decode("utf-8", "replace")
    if reason:
        if reason == "cancelled" and not stderr:
            stderr = "ocr review stopped after cancellation"
        return OcrRun(
            exit_code=124,
            stdout=stdout,
            stderr=stderr,
            timed_out=reason == "timeout",
            idle_timed_out=reason == "idle",
        )
    return OcrRun(exit_code=proc.returncode or 0, stdout=stdout, stderr=stderr)


def _call_accepts_keyword(callable_obj: Callable[..., object], keyword: str) -> bool:
    try:
        parameters = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    return keyword in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


async def _select_bounded(
    select: Callable[..., tuple[Any, tuple[Any, ...]]],
    requirements: TaskRequirements,
    *,
    now: datetime,
    timeout_seconds: float,
) -> tuple[Any | None, tuple[Any, ...]] | None:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(select, requirements, now=now), timeout_seconds
        )
    except asyncio.TimeoutError:
        return None


async def _run_runner_bounded(
    runner: OcrRunner, argv: Sequence[str], *, env: Mapping[str, str], timeout_seconds: float
) -> OcrRun:
    runner_timeout = executor_timeout_within(timeout_seconds) if timeout_seconds > 0 else 0.0
    runner_timeout = max(0.0, runner_timeout)
    settle_budget = max(0.0, timeout_seconds - runner_timeout)
    stop_event = threading.Event()
    accepts_stop_event = _call_accepts_keyword(cast(Callable[..., object], runner), "stop_event")

    def call_runner() -> OcrRun:
        if accepts_stop_event:
            return cast(Callable[..., OcrRun], runner)(
                argv, env=env, timeout=runner_timeout, stop_event=stop_event
            )
        return runner(argv, env=env, timeout=runner_timeout)

    task = asyncio.create_task(asyncio.to_thread(call_runner))

    async def request_stop_and_wait(*, preserve_provider_timeout: bool) -> OcrRun:
        stop_event.set()
        cleanup_wait = min(_RUNNER_SETTLE_SECONDS, settle_budget)
        try:
            run = await asyncio.wait_for(asyncio.shield(task), cleanup_wait)
        except asyncio.TimeoutError:
            return OcrRun(exit_code=124, stderr="ocr review cleanup UNKNOWN after cancellation")
        if preserve_provider_timeout and run.timed_out and "stopped" not in run.stderr.lower():
            return run
        return OcrRun(
            exit_code=run.exit_code,
            stdout=run.stdout,
            stderr="ocr review stopped after cancellation",
            timed_out=False,
            idle_timed_out=run.idle_timed_out,
        )

    try:
        return await asyncio.wait_for(asyncio.shield(task), runner_timeout)
    except asyncio.TimeoutError:
        return await request_stop_and_wait(preserve_provider_timeout=True)
    except asyncio.CancelledError:
        return await request_stop_and_wait(preserve_provider_timeout=False)


_subprocess_runner = make_subprocess_runner()


# OCR v1.12.9 ``default_path`` drops ordinary test files unless ``--rule``
# ``include`` names them. Broad recursive test globs also re-admit
# fixture/testdata/generated paths that OCR otherwise keeps out, so production
# must include only the *exact* changed paths that look like ordinary tests and
# are not under those protected directories. Include stays additive; omit
# ``exclude`` so custom/security excludes still merge with OCR defaults.
_OCR_PROTECTED_PATH_PARTS: frozenset[str] = frozenset(
    {"fixtures", "testdata", "generated", "__fixtures__"}
)

# Basename / path heuristics mirroring OCR's ordinary test default_path set,
# without duplicating OCR's full policy list as recursive globs.
_OCR_ORDINARY_TEST_BASENAME_SUFFIXES: tuple[str, ...] = (
    "_test.py",
    "_test.go",
    "_test.rs",
    "_test.zig",
    "Test.java",
    "Tests.java",
    "Test.swift",
    "Tests.swift",
    "_spec.rb",
    "Spec.hs",
    "Spec.lhs",
    "Test.fs",
)

_OCR_ORDINARY_TEST_DOT_SUFFIXES: tuple[str, ...] = (
    ".test.js",
    ".test.jsx",
    ".test.ts",
    ".test.tsx",
    ".spec.js",
    ".spec.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".test.ets",
)


def _posix_path_parts(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.replace("\\", "/").split("/") if part and part != ".")


def _is_ocr_protected_path(path: str) -> bool:
    """True when a changed path sits under OCR-protected fixture/testdata/generated dirs."""
    parts = {part.lower() for part in _posix_path_parts(path)}
    return bool(parts & {p.lower() for p in _OCR_PROTECTED_PATH_PARTS})


def _looks_like_ordinary_ocr_test_path(path: str) -> bool:
    """Heuristic for ordinary tests OCR would drop via default_path.

    Intentionally narrower than OCR's full default_path list: only common
    test naming that we need to re-include for changed files. Protected
    directories are rejected separately.
    """
    if _is_ocr_protected_path(path):
        return False
    norm = path.replace("\\", "/").strip().rstrip("/")
    name = norm.rsplit("/", 1)[-1]
    # Directory-only / empty basename entries are not include targets.
    if not name or "." not in name:
        return False
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if any(name.endswith(suffix) for suffix in _OCR_ORDINARY_TEST_BASENAME_SUFFIXES):
        return True
    if any(name.endswith(suffix) for suffix in _OCR_ORDINARY_TEST_DOT_SUFFIXES):
        return True
    parts = _posix_path_parts(norm)
    lower_parts = {p.lower() for p in parts}
    # ``__tests__/`` trees are ordinary JS/TS tests unless under fixtures/…
    if "__tests__" in lower_parts:
        return True
    # ``src/test/java`` / ``src/test`` Kotlin layouts
    return bool(
        "src" in lower_parts and "test" in lower_parts and name.endswith((".java", ".kt", ".kts"))
    )


def _changed_ordinary_test_paths(
    *, repo: Path, base_ref: str, head_ref: str, differ: DiffLister
) -> list[str]:
    """Exact changed paths that should be OCR ``include`` entries."""
    selected: list[str] = []
    seen: set[str] = set()
    try:
        changed = differ(repo=repo, base_ref=base_ref, head_ref=head_ref)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Keep review running; empty include preserves OCR default_path behavior.
        return []
    for raw in changed:
        path = raw.replace("\\", "/").strip()
        if not path or path in seen:
            continue
        if _looks_like_ordinary_ocr_test_path(path):
            selected.append(path)
            seen.add(path)
    return selected


def _write_ocr_changed_test_include_rule(directory: Path, include_paths: Sequence[str]) -> Path:
    """Write an ephemeral OCR ``--rule`` with exact changed-test includes.

    Lives under the review ``out_dir`` (same lifecycle as ``ocr-raw.json``) and
    never touches ``~/.opencodereview``. No ``exclude`` key: OCR merges custom
    excludes itself; we only add the needed includes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "verdict-ocr-changed-test-include.json"
    payload: dict[str, object] = {"include": list(include_paths)}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class OpenCodeReviewer:
    """Independent, fail-closed semantic reviewer backed by the ``ocr`` CLI."""

    _REVIEWER_PREFIX = "open-code-review"
    _PROVIDER = "verdict-omniroute"

    def __init__(
        self,
        selector: ModelSelector,
        *,
        ocr_bin: str = "ocr",
        gateway_url: str = "http://127.0.0.1:20128/v1",
        api_key_env: str = "VERDICT_OMNIROUTE_API_KEY",
        effort: str = "low",
        timeout_seconds: float = 900,
        token_budget: int = 400_000,
        max_reviewer_attempts: int = 3,
        out_dir: Path,
        runner: OcrRunner | None = None,
        idle_timeout_seconds: float | None = 300,
        differ: DiffLister | None = None,
    ) -> None:
        self._selector = selector
        self._ocr_bin = ocr_bin
        self._gateway_url = gateway_url
        self._api_key_env = api_key_env
        self._effort = effort
        self._timeout_seconds = float(timeout_seconds)
        self._token_budget = int(token_budget)
        self._max_reviewer_attempts = max(1, int(max_reviewer_attempts))
        self._out_dir = Path(out_dir)
        self._idle_timeout_seconds = idle_timeout_seconds
        self._runner: OcrRunner = runner or make_subprocess_runner(idle_timeout_seconds)
        self._differ: DiffLister = differ or _git_diff_names

    async def review(
        self,
        *,
        repo: Path,
        base_ref: str,
        head_ref: str,
        background: str,
        exclude_routes: frozenset[str],
        exclude_families: frozenset[str],
        timeout_seconds: float | None = None,
    ) -> ReviewResult:
        now = datetime.now(timezone.utc)
        budget = self._timeout_seconds
        if timeout_seconds is not None:
            budget = min(budget, executor_timeout_within(float(timeout_seconds)))
        review_deadline = time.monotonic() + budget
        requirements = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            coding=True,
            reasoning=True,
            frontier_worthy=True,
            exclude_routes=exclude_routes,
            exclude_families=exclude_families | aliased_pools_for(exclude_routes),
        )
        tried: set[str] = set()
        last: ReviewResult | None = None
        attempts: list[dict[str, object]] = []
        for _attempt in range(self._max_reviewer_attempts):
            started = time.monotonic()
            remaining = review_deadline - started
            if remaining <= 0:
                return ReviewResult(
                    status="ERROR",
                    reviewer=self._REVIEWER_PREFIX,
                    route_id="",
                    detail="reviewer selection timed out; selector ownership/work unknown",
                    attempts=(
                        {
                            "route_id": "",
                            "status": "ERROR",
                            "category": "selector_timeout",
                            "scope": "none",
                            "duration_seconds": 0.0,
                        },
                    ),
                )
            selection = await _select_bounded(
                self._selector.select,
                replace(requirements, exclude_routes=frozenset(exclude_routes | tried)),
                now=now,
                timeout_seconds=executor_timeout_within(remaining),
            )
            if selection is None:
                return ReviewResult(
                    status="ERROR",
                    reviewer=self._REVIEWER_PREFIX,
                    route_id="",
                    detail="reviewer selection timed out; selector ownership/work unknown",
                    attempts=(
                        {
                            "route_id": "",
                            "status": "ERROR",
                            "category": "selector_timeout",
                            "scope": "none",
                            "duration_seconds": round(time.monotonic() - started, 3),
                        },
                    ),
                )
            chosen, _verdicts = selection
            if chosen is None:
                break
            # Launch gate: the reviewer route must be proven healthy or confirmed
            # live before OCR runs on it. Raises AdmissionBypassError on violation.
            require_launchable(self._selector, chosen.route_id, surface="OpenCodeReviewer.launch")
            # BOD-223/224: re-check launch-critical evidence immediately before
            # OCR launches; a reviewer cooled since selection is not launched.
            blocker = dispatch_blocker(self._selector, chosen.route_id, datetime.now(timezone.utc))
            if blocker is not None:
                attempts.append(
                    {
                        "route_id": chosen.route_id,
                        "status": "REVOKED",
                        "category": "pre_dispatch_revoked",
                        "detail": f"pre-dispatch recheck: {blocker} cooling",
                        "duration_seconds": 0.0,
                    }
                )
                tried.add(chosen.route_id)
                continue
            started = time.monotonic()
            remaining = review_deadline - started
            if remaining <= 0:
                last = ReviewResult(
                    status="ERROR",
                    reviewer=self._REVIEWER_PREFIX,
                    route_id=chosen.route_id,
                    detail="ocr review timed out",
                )
                break
            result = await self._review_once(
                chosen.route_id,
                repo=repo,
                base_ref=base_ref,
                head_ref=head_ref,
                background=background,
                timeout_seconds=remaining,
            )
            if result.status != "ERROR" or not self._provider_failure(result):
                attempts.append(
                    {
                        "route_id": chosen.route_id,
                        "status": result.status,
                        "category": "",
                        "scope": "none",
                        "duration_seconds": round(time.monotonic() - started, 3),
                    }
                )
                return replace(result, attempts=tuple(attempts))
            # The REVIEWER's provider failed (504/timeout/quota/identity), not the
            # review: classify it with the shared failure policy, cool the
            # correct scope, and reselect another independent reviewer.
            failure = self._classify_failure(chosen.route_id, result, now=now)
            attempts.append(
                {
                    "route_id": chosen.route_id,
                    "status": result.status,
                    "category": failure.category,
                    "scope": failure.scope,
                    "cooldown_seconds": failure.cooldown_seconds,
                    "detail": result.detail[:200],
                    "duration_seconds": round(time.monotonic() - started, 3),
                }
            )
            last = result
            tried.add(chosen.route_id)
            if failure.scope != "none" and failure.cooldown_seconds > 0:
                self._selector.record_failure(chosen.route_id, failure, now=now)
        if last is not None:
            return replace(
                last,
                detail=f"{last.detail}; reviewer pool exhausted after {len(tried)}",
                attempts=tuple(attempts),
            )
        return ReviewResult(
            status="ERROR",
            reviewer=self._REVIEWER_PREFIX,
            route_id="",
            detail="no independent reviewer eligible",
            attempts=tuple(attempts),
        )

    @staticmethod
    def _classify_failure(
        route_id: str, result: ReviewResult, *, now: datetime
    ) -> FailureClassification:
        """Reviewer failures use the same policy as workers (BOD-224).

        A provider 429/401/402 cools the whole provider; a timeout or 5xx cools
        the route; an identity mismatch is route-scoped. An unclassifiable
        reviewer error keeps the previous conservative route cooldown.
        """
        from verdict.orchestration.contracts import WorkerTerminal
        from verdict.orchestration.recovery import FailureIntelligence

        detail = result.detail
        if detail.startswith("identity_mismatch"):
            return FailureClassification("model_mismatch", "REROUTE", 3600, "route", detail)
        match = re.search(r"(?<!\d)(4\d\d|5\d\d)(?!\d)", detail)
        terminal = WorkerTerminal(
            ok=False,
            model=route_id,
            error=detail,
            status_code=int(match.group(1)) if match else None,
            stop_reason="timeout" if "timed out" in detail.lower() else "error",
        )
        failure = FailureIntelligence().classify(terminal, now=now)
        if failure.category in {"unknown", "no_final_answer"}:
            return FailureClassification("upstream_temporary", "REROUTE", 300, "route", detail)
        return failure

    async def _review_once(
        self,
        route_id: str,
        *,
        repo: Path,
        base_ref: str,
        head_ref: str,
        background: str,
        timeout_seconds: float,
    ) -> ReviewResult:

        self._out_dir.mkdir(parents=True, exist_ok=True)
        raw_path = self._out_dir / "ocr-raw.json"
        home_dir = self._out_dir / f".ocr-home-{uuid.uuid4().hex}"
        try:
            api_key = self._read_api_key()
        except KeyError as exc:
            return ReviewResult(
                status="ERROR", reviewer=self._REVIEWER_PREFIX, route_id=route_id, detail=str(exc)
            )

        try:
            self._write_isolated_config(home_dir, api_key)
            # Ephemeral --rule under review out_dir (same lifecycle as ocr-raw;
            # isolated HOME still holds OCR config and is deleted after).
            # Include exact changed ordinary tests only; no exclude key.
            include_paths = _changed_ordinary_test_paths(
                repo=repo, base_ref=base_ref, head_ref=head_ref, differ=self._differ
            )
            rule_path = _write_ocr_changed_test_include_rule(self._out_dir, include_paths)
            argv = [
                self._ocr_bin,
                "review",
                "--repo",
                str(repo),
                "--from",
                base_ref,
                "--to",
                head_ref,
                "--format",
                "json",
                "--audience",
                "agent",
                "--effort",
                self._effort,
                "--max-tokens-budget",
                str(self._token_budget),
                "--provider",
                self._PROVIDER,
                "--model",
                route_id,
                "--background",
                background,
                "--output",
                str(raw_path),
                "--rule",
                str(rule_path),
            ]
            run = await _run_runner_bounded(
                self._runner, argv, env={"HOME": str(home_dir)}, timeout_seconds=timeout_seconds
            )
        finally:
            shutil.rmtree(home_dir, ignore_errors=True)

        return self._interpret(run, raw_path, route_id)

    # -- internals ---------------------------------------------------------

    def _read_api_key(self) -> str:
        if self._api_key_env != "VERDICT_OMNIROUTE_API_KEY":
            value = os.environ.get(self._api_key_env)
            if value:
                return value
        resolved = resolve_gateway_key()
        if resolved is not None:
            return resolved[0]
        raise KeyError(f"missing API key env {self._api_key_env}")

    def _write_isolated_config(self, home_dir: Path, api_key: str) -> None:
        cfg_dir = home_dir / ".opencodereview"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "custom_providers": {
                self._PROVIDER: {"url": self._gateway_url, "protocol": "openai", "api_key": api_key}
            },
            "llm": {},
        }
        (cfg_dir / "config.json").write_text(json.dumps(config, indent=2))

    def _interpret(self, run: OcrRun, raw_path: Path, route_id: str) -> ReviewResult:
        raw_ref = str(raw_path)
        if "ocr review cleanup UNKNOWN" in run.stderr:
            return self._error(route_id, raw_ref, run.stderr)
        if "ocr review stopped after cancellation" in run.stderr:
            return self._error(route_id, raw_ref, run.stderr)
        if run.exit_code == 125 and (
            "owned worker launch refused" in run.stderr
            or "owned review launch refused" in run.stderr
        ):
            return self._error(route_id, raw_ref, run.stderr)
        if run.timed_out:
            return self._error(route_id, raw_ref, "ocr review timed out")
        if run.idle_timed_out:
            # Worded so the shared failure policy classifies it as a timeout.
            return self._error(
                route_id,
                raw_ref,
                f"ocr review timed out: no progress for {self._idle_timeout_seconds}s",
            )
        if run.exit_code != 0:
            return self._error(route_id, raw_ref, f"ocr review exited {run.exit_code}")
        if not raw_path.exists():
            return self._error(route_id, raw_ref, "ocr output file missing")
        try:
            payload = json.loads(raw_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return self._error(route_id, raw_ref, f"unparseable ocr output: {exc}")
        if not isinstance(payload, dict):
            return self._error(route_id, raw_ref, "ocr output was not an object")

        empty_reason = self._empty_review_reason(payload)
        if empty_reason is not None:
            # A skipped/empty review is an input/coverage failure, not a provider
            # outage. Changing reviewer cannot repair an empty selected range.
            return self._error(route_id, raw_ref, empty_reason)

        if self._every_item_failed(payload):
            return self._error(route_id, raw_ref, "every reviewed item failed")

        observed = self._observed_model(payload)
        if observed is not None and observed.removeprefix("omniroute/") != route_id.removeprefix(
            "omniroute/"
        ):
            # Fail closed on substitution: the review ran on a model Verdict did
            # not select, so it is not evidence from the selected reviewer.
            return self._error(
                route_id, raw_ref, f"identity_mismatch: selected {route_id}, observed {observed}"
            )
        findings = self._parse_findings(payload)
        reviewer = self._reviewer_label(payload)
        status = "FAIL" if any(f.blocking() for f in findings) else "PASS"
        return ReviewResult(
            status=status,
            reviewer=reviewer,
            route_id=route_id,
            findings=tuple(findings),
            raw_ref=raw_ref,
            observed_model=observed or "",
        )

    @staticmethod
    def _observed_model(payload: Mapping[str, object]) -> str | None:
        manifest = payload.get("manifest")
        execution = manifest.get("execution") if isinstance(manifest, Mapping) else None
        model = execution.get("model") if isinstance(execution, Mapping) else None
        return str(model).strip() or None if model else None

    @staticmethod
    def _provider_failure(result: ReviewResult) -> bool:
        detail = result.detail.lower()
        return any(
            token in detail
            for token in (
                "identity_mismatch",
                "exited",
                "timed out",
                "provider",
                "504",
                "429",
                "503",
            )
        )

    def _error(self, route_id: str, raw_ref: str, detail: str) -> ReviewResult:
        return ReviewResult(
            status="ERROR",
            reviewer=self._REVIEWER_PREFIX,
            route_id=route_id,
            raw_ref=raw_ref,
            detail=detail,
        )

    @staticmethod
    def _empty_review_reason(payload: Mapping[str, object]) -> str | None:
        """Check for explicit coverage vetoes, then require positive evidence.

        Returns an ERROR detail when the review was explicitly skipped or lacks
        positive coverage, otherwise None to proceed to findings analysis.

        Precedence (Design § 1):
        1. Explicit vetoes: skipped status/terminal, empty selected list,
           invalid/nonpositive file count
        2. Require at least one positive signal: non-empty selected list OR
           non-empty completed list OR positive files_reviewed count
        3. If no positive signal exists, return "review coverage missing"
        """
        manifest = payload.get("manifest")
        terminal = manifest.get("terminal_state") if isinstance(manifest, Mapping) else None

        # Veto 1: Explicit skipped status or terminal state
        if any(
            str(value).strip().lower() == "skipped" for value in (payload.get("status"), terminal)
        ):
            return "review skipped: no items reviewed"

        coverage = manifest.get("coverage") if isinstance(manifest, Mapping) else None

        # Veto 2: Explicitly empty selected list (Design § 1, precedence example)
        if isinstance(coverage, Mapping) and coverage.get("selected") == []:
            return "review skipped: no items selected"

        summary = payload.get("summary")

        # Veto 3: Invalid file count
        if isinstance(summary, Mapping) and "files_reviewed" in summary:
            try:
                count = int(summary["files_reviewed"])
            except (ValueError, TypeError, OverflowError):
                return "review coverage invalid: files_reviewed is not a count"
            # Veto 4: Zero or negative file count
            if count <= 0:
                return "review skipped: zero files reviewed"

        # After vetoes, check for positive evidence (Design § 1)
        # Positive signal 1: non-empty selected list
        has_selected = (
            isinstance(coverage, Mapping)
            and isinstance(coverage.get("selected"), list)
            and len(coverage.get("selected", [])) > 0
        )

        # Positive signal 2: non-empty completed list
        has_completed = (
            isinstance(coverage, Mapping)
            and isinstance(coverage.get("completed"), list)
            and len(coverage.get("completed", [])) > 0
        )

        # Positive signal 3: valid positive files_reviewed count
        # (already validated above; if we reach here it's either absent or valid)
        has_positive_count = False
        if isinstance(summary, Mapping) and "files_reviewed" in summary:
            try:
                count = int(summary["files_reviewed"])
                has_positive_count = count > 0
            except (ValueError, TypeError, OverflowError):
                pass  # Already caught as veto above

        # Require at least one positive signal (Design § 1, AC 2)
        if not (has_selected or has_completed or has_positive_count):
            return "review coverage missing"

        return None

    @staticmethod
    def _every_item_failed(payload: Mapping[str, object]) -> bool:
        manifest = payload.get("manifest")
        if not isinstance(manifest, Mapping):
            return False
        coverage = manifest.get("coverage")
        if not isinstance(coverage, Mapping):
            return False
        selected = coverage.get("selected")
        failed = coverage.get("failed")
        if not isinstance(selected, list) or not selected:
            return False
        if not isinstance(failed, list):
            return False
        return len(failed) >= len(selected)

    @staticmethod
    def _parse_findings(payload: Mapping[str, object]) -> list[ReviewFinding]:
        comments = payload.get("comments")
        findings: list[ReviewFinding] = []
        if not isinstance(comments, list):
            return findings
        for item in comments:
            if not isinstance(item, Mapping):
                continue
            line_raw = item.get("start_line", item.get("line"))
            line: int | None
            try:
                line = int(line_raw) if line_raw is not None else None
            except (TypeError, ValueError):
                line = None
            findings.append(
                ReviewFinding(
                    severity=_normalize_severity(item.get("severity")),
                    category=str(item.get("category", "") or ""),
                    file=str(item.get("path", item.get("file", "")) or ""),
                    line=line,
                    message=str(item.get("content", item.get("message", "")) or ""),
                )
            )
        return findings

    def _reviewer_label(self, payload: Mapping[str, object]) -> str:
        version = self._resolve_version(payload)
        return f"{self._REVIEWER_PREFIX} {version}".strip()

    def _resolve_version(self, payload: Mapping[str, object]) -> str:
        manifest = payload.get("manifest")
        if isinstance(manifest, Mapping):
            execution = manifest.get("execution")
            if isinstance(execution, Mapping):
                version = execution.get("ocr_version")
                if version:
                    return str(version)
        return self._probe_version()

    def _probe_version(self) -> str:
        run = self._runner([self._ocr_bin, "--version"], env={}, timeout=30)
        first = (run.stdout or run.stderr).strip().splitlines()
        if not first:
            return "unknown"
        # e.g. "open-code-review v1.12.9 (bccbc15) linux/amd64"
        parts = first[0].split()
        for token in parts:
            if token.startswith("v") and token[1:2].isdigit():
                return token
        return parts[-1] if parts else "unknown"


class DiffLister(Protocol):
    def __call__(self, *, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]: ...


def _git_diff_names(*, repo: Path, base_ref: str, head_ref: str) -> Sequence[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", f"{base_ref}..{head_ref}"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in completed.stdout.splitlines() if line.strip()]
