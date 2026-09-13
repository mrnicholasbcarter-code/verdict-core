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
import time
import types
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any


def load_state() -> types.ModuleType:
    """Reuse the workflow contract module instead of duplicating its rules."""
    path = Path(__file__).resolve().parent / "prime_state.py"
    spec = importlib.util.spec_from_file_location("prime_state_contracts", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load workflow contracts from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STATE = load_state()


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


def run_attempt(
    command: list[str],
    cwd: Path,
    log: Path,
    fingerprint: Callable[[], Any],
    idle_seconds: float,
    timeout: float,
    poll: float = 5,
    progress_made: Callable[[Any, Any], bool] | None = None,
) -> dict[str, Any]:
    started = progress = time.monotonic()
    previous = fingerprint()
    made_progress = progress_made or (lambda old, new: old != new)
    with log.open("w") as output:
        process = subprocess.Popen(
            command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
        )
        try:
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
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--idle-seconds", type=float, default=600)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--poll-seconds", type=float, default=5, help="Watchdog poll interval")
    parser.add_argument("--max-restarts", type=int, default=2)
    parser.add_argument("--max-issues", type=int, default=5)
    args = parser.parse_args()
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
        atomic_json(
            state / "run.json",
            {
                "run_id": token,
                "max_issues": args.max_issues,
                "started_at": time.time(),
                "state_dir": str(state),
            },
        )
        prompt = (
            f"/skill:verdict-resume\nSupervisor run_id={token}; state_dir={state}. "
            f"Budget {args.max_issues} issues across this supervisor run, {args.timeout}s per session. "
            "Read checkpoint.json and supervisor.json before any action. Reconcile authorities. "
            "This is a fresh context recovery, not permission to repeat completed work. "
            f"Use synchronous child subprocesses with --session-dir {session_dir}; do not detach RLM writers. "
            "Before exit atomically write outcome.json with this run_id, status DONE/IDLE/BLOCKED, "
            "and reason. Preserve progress in checkpoint.json before compacting or returning."
        )
        try:
            result = run_attempt(
                [
                    args.prime,
                    "--cwd",
                    str(repo),
                    "--provider",
                    args.provider,
                    "--model",
                    args.model,
                    "--session-dir",
                    str(session_dir),
                    "-p",
                    "--mode",
                    "json",
                    prompt,
                ],
                repo,
                state / f"session-{token}.log",
                lambda: workspace_fingerprint(repo, state),
                args.idle_seconds,
                args.timeout,
                poll=args.poll_seconds,
                progress_made=progress_made,
            )
        finally:
            stop_owned_daemon(args.prime, session_dir)
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
                return {"reason": "MISSING_OUTCOME", "returncode": 1}
            if outcome["status"] == "BLOCKED":
                # Deliberate block needs an external change, never repeat it automatically.
                raise ValueError(f"Prime blocked: {outcome['reason']}")
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
                return recover(attempt, state, args.max_restarts)
            except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
                atomic_json(
                    state / "supervisor.json",
                    {
                        "status": "BLOCKED",
                        "attempts": count,
                        "reason": str(exc),
                        "observed_at": time.time(),
                    },
                )
                raise
    except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        print(f"BLOCKED: {exc}", flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
