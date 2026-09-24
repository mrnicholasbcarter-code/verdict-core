"""External supervisor that keeps the orchestration controller process alive.

BOD-159..166 (minimal interview-safe slice). The controller is a *process*
(``python -m verdict orchestrate ... --resume <run_id>``) that publishes liveness
in ``<run_dir>/progress.json`` and truth in ``<run_dir>/events.jsonl``. A
controller killed by its own model quota cannot restart itself, so it is
supervised from the outside: spawn generation N in its own process group; watch
exit, a stalled ``progress.json``, and capacity-failure markers in the log tail
(classified with ``recovery.FailureIntelligence``); kill the whole process group
(SIGTERM, then SIGKILL) and restart with ``command_factory(generation + 1)``, so
the caller may replace the controller configuration on each life while
``--resume`` keeps already VALIDATED nodes; stop on ``run_finished``; fail closed
(``FAILED_CLOSED`` + a ``run_finished`` BLOCKED event) once the restart budget or
the total deadline is spent. Every wait is bounded.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, ClassVar, Protocol

from verdict.orchestration.contracts import FailureClassification, FailureClassifier, WorkerTerminal

EVENTS_FILE = "events.jsonl"
PROGRESS_FILE = "progress.json"
REDACTED = "[REDACTED]"
TAIL_BYTES = 8192
QUOTA_CATEGORIES = frozenset({"quota_exhausted", "rate_limited"})

# Controller-capacity death markers, matched case-insensitively on the log tail.
CAPACITY_MARKERS: tuple[str, ...] = (
    "429",
    "usage limit",
    "quota",
    "rate limit",
    "insufficient_quota",
    "chat_admission_busy",
    "401",
    "402",
    "403",
    "traceback",
)
_STATUS_CODES: tuple[int, ...] = (429, 401, 402, 403, 404, 500, 502, 503, 504)


def read_events(path: Path) -> list[dict[str, Any]]:
    """Tolerant event reader: a torn or partial tail line is ignored, never fatal."""
    events: list[dict[str, Any]] = []
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                value = json.loads(line) if line.strip() else None
                if isinstance(value, dict):
                    events.append(value)
    return events


def redact_argv(argv: Sequence[str]) -> str:
    """Argv summary safe for the event log: no API keys, no ``sk-`` tokens."""
    out: list[str] = []
    hide_next = False
    for token in argv:
        if hide_next:
            out.append(REDACTED)
            hide_next = False
        elif token == "--api-key":
            out.append(token)
            hide_next = True
        elif token.startswith("--api-key="):
            out.append("--api-key=" + REDACTED)
        else:
            out.append(REDACTED if "sk-" in token else token)
    return " ".join(out)


def _load(module_name: str, attribute: str) -> Any:
    """Optional sibling module attribute; ``None`` when that module is not importable."""
    with contextlib.suppress(Exception):
        return getattr(importlib.import_module(module_name), attribute, None)
    return None


def emit_event(path: Path, type: str, /, **data: Any) -> None:
    """Append one event, preferring ``receipt.EventLog``; never raises."""
    factory = _load("verdict.orchestration.receipt", "EventLog")
    if factory is not None:
        with contextlib.suppress(Exception):
            factory(path).emit(type, **data)
            return
    seq = max((int(e.get("seq", 0)) for e in read_events(path)), default=0) + 1
    at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    event = {"seq": seq, "at": at, "type": type, "node_id": "", "data": data}
    with contextlib.suppress(OSError), path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class _FallbackClassifier:
    """Stand-in used when ``recovery.FailureIntelligence`` is not importable."""

    _NAMED: ClassVar[dict[int, str]] = {
        401: "authentication",
        402: "payment_required",
        403: "permission",
    }

    def classify(self, terminal: WorkerTerminal, *, now: datetime) -> FailureClassification:
        del now
        text = (terminal.error or "").lower()
        quota = any(m in text for m in ("quota", "usage limit", "insufficient_quota"))
        if quota or terminal.status_code == 429 or "429" in text or "rate limit" in text:
            name = "quota_exhausted" if quota else "rate_limited"
            return FailureClassification(name, "REROUTE", 3600.0, "provider", text[:300])
        name = self._NAMED.get(terminal.status_code or 0, "unknown")
        return FailureClassification(name, "REROUTE", 120.0, "provider", text[:300])


def default_classifier() -> FailureClassifier:
    """``verdict.orchestration.recovery.FailureIntelligence`` when available."""
    factory = _load("verdict.orchestration.recovery", "FailureIntelligence")
    if factory is None:
        return _FallbackClassifier()
    classifier: FailureClassifier = factory()
    return classifier


class SupervisedProcess(Protocol):
    """Only what the supervisor needs from a controller child process."""

    @property
    def pid(self) -> int | None: ...

    @property
    def returncode(self) -> int | None: ...

    def wait(self) -> Awaitable[int]: ...


class Spawner(Protocol):
    def __call__(
        self, argv: list[str], *, log_path: Path, env: Mapping[str, str] | None
    ) -> Awaitable[SupervisedProcess]: ...


@dataclass(frozen=True)
class SupervisorOutcome:
    state: str  # COMPLETE | BLOCKED
    reason: str
    restarts: int
    generations: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _Watch:
    kind: str  # FINISHED | STALLED | EXITED | DEADLINE
    outcome: str = ""
    reason: str = ""


class ControllerSupervisor:
    """Supervise one controller run directory across successive controller lives."""

    def __init__(
        self,
        command_factory: Callable[[int], list[str]],
        run_dir: Path,
        *,
        stall_seconds: float = 300.0,
        poll_seconds: float = 5.0,
        max_restarts: int = 2,
        total_deadline_seconds: float = 7200.0,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        spawner: Spawner | None = None,
        kill_grace_seconds: float = 10.0,
        classifier: FailureClassifier | None = None,
    ) -> None:
        self.command_factory, self.run_dir = command_factory, Path(run_dir)
        self.stall_seconds, self.poll_seconds = float(stall_seconds), max(poll_seconds, 0.001)
        self.max_restarts, self.total_deadline_seconds = max_restarts, total_deadline_seconds
        self.env = dict(env) if env is not None else None
        self.kill_grace_seconds = float(kill_grace_seconds)
        self.classifier = classifier or default_classifier()
        self.generations: list[dict[str, Any]] = []
        self._clock, self._sleep, self._spawner = clock, sleep, spawner
        self.run_dir.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------- run state
    def _emit(self, **data: Any) -> None:
        emit_event(self.run_dir / EVENTS_FILE, "controller", **data)

    def _finished(self) -> _Watch | None:
        """The run's own verdict, once the controller wrote ``run_finished``."""
        for event in reversed(read_events(self.run_dir / EVENTS_FILE)):
            if event.get("type") == "run_finished":
                raw = event.get("data")
                data = raw if isinstance(raw, dict) else {}
                return _Watch(
                    "FINISHED", str(data.get("outcome") or ""), str(data.get("reason") or "")
                )
        return None

    def _progress(self) -> tuple[int, str]:
        """``(seq, last_progress_at)`` from ``progress.json``; ``(-1, "")`` when unreadable."""
        try:
            value = json.loads((self.run_dir / PROGRESS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return (-1, "")
        if not isinstance(value, dict):
            return (-1, "")
        seq = value.get("seq", -1)
        return (seq if isinstance(seq, int) else -1, str(value.get("last_progress_at", "")))

    def _tail(self, log_path: Path) -> str:
        try:
            with log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - TAIL_BYTES), os.SEEK_SET)
                return stream.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    def _scan(self, record: dict[str, Any], log_path: Path) -> None:
        """Classify controller-capacity markers found in the controller log tail."""
        text = self._tail(log_path)
        lowered = text.lower()
        marker = next((m for m in CAPACITY_MARKERS if m in lowered), None)
        if marker is None:
            return
        status = next((c for c in _STATUS_CODES if str(c) in lowered), None)
        terminal = WorkerTerminal(ok=False, error=text[-TAIL_BYTES:], status_code=status)
        now = datetime.now(timezone.utc)
        record["marker"] = marker
        record["category"] = self.classifier.classify(terminal, now=now).category

    # -------------------------------------------------------------- processes
    async def _spawn(self, argv: list[str], log_path: Path) -> tuple[SupervisedProcess, IO[bytes]]:
        stream = log_path.open("ab")
        if self._spawner is not None:
            child_env = dict(self.env) if self.env is not None else None
            return await self._spawner(argv, log_path=log_path, env=child_env), stream
        environment = {**os.environ, **self.env} if self.env is not None else None
        merged = asyncio.subprocess.STDOUT
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=stream, stderr=merged, start_new_session=True, env=environment
        )
        return process, stream

    def _signal(self, process: SupervisedProcess, sig: int) -> None:
        """Signal the whole process group so grandchildren die with the controller."""
        pid = process.pid
        if pid is not None and pid > 0:
            try:
                os.killpg(os.getpgid(pid), sig)
                return
            except OSError:
                pass
        fallback = getattr(process, "kill" if sig == signal.SIGKILL else "terminate", None)
        if callable(fallback):
            with contextlib.suppress(Exception):
                fallback()

    async def _kill(self, process: SupervisedProcess, waiter: asyncio.Future[int]) -> None:
        """SIGTERM the group, SIGKILL after the grace window. Always bounded."""
        if waiter.done():
            with contextlib.suppress(Exception):
                waiter.result()
            return
        self._signal(process, signal.SIGTERM)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(waiter), self.kill_grace_seconds)
        if not waiter.done():
            self._signal(process, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(waiter), 5.0)
        if not waiter.done():
            waiter.cancel()

    async def _watch(
        self, record: dict[str, Any], log_path: Path, waiter: asyncio.Future[int], deadline: float
    ) -> _Watch:
        """Bounded liveness loop for one controller life."""
        last_progress, last_advance = (-2, ""), self._clock()
        while True:
            finished = self._finished()
            if finished is not None:
                return finished
            self._scan(record, log_path)
            progress = self._progress()
            if progress != last_progress:
                last_progress, last_advance = progress, self._clock()
            if waiter.done():
                self._scan(record, log_path)
                exited = _Watch("EXITED", reason="controller exited without run_finished")
                return self._finished() or exited
            now = self._clock()
            if now >= deadline:
                return _Watch("DEADLINE", reason="total deadline exceeded")
            if now - last_advance >= self.stall_seconds:
                return _Watch("STALLED", reason=f"no progress for {self.stall_seconds:g}s")
            await self._sleep(self.poll_seconds)

    # -------------------------------------------------------------- main loop
    async def run(self) -> SupervisorOutcome:
        deadline = self._clock() + self.total_deadline_seconds
        generation, restarts = 0, 0
        while True:
            argv = list(self.command_factory(generation))
            if not argv:
                return self._fail_closed("command_factory returned an empty command", restarts)
            summary = redact_argv(argv)
            if generation:
                self._emit(state="REPLACED", generation=generation, argv=summary, detail=summary)
            log_path = self.run_dir / f"controller-g{generation}.log"
            record: dict[str, Any] = {"generation": generation, "argv": summary}
            record.update(log=log_path.name, pid=None, state="RUNNING", category="", exit_code=None)
            self.generations.append(record)
            watch = await self._supervise_one(record, log_path, argv, deadline)
            if watch.kind == "FINISHED":
                state = "COMPLETE" if watch.outcome == "COMPLETE" else "BLOCKED"
                record["state"] = state
                reason = watch.reason or f"controller reported {state}"
                return SupervisorOutcome(state, reason, restarts, self.generations)
            if watch.kind == "DEADLINE":
                record["state"] = "DEADLINE"
                return self._fail_closed(
                    f"total deadline of {self.total_deadline_seconds:g}s exceeded", restarts
                )
            self._note_death(record, watch, generation)
            if restarts >= self.max_restarts:
                budget = f"restart budget ({self.max_restarts}) exhausted"
                return self._fail_closed(f"controller {record['state']} and {budget}", restarts)
            restarts, generation = restarts + 1, generation + 1

    async def _supervise_one(
        self, record: dict[str, Any], log_path: Path, argv: list[str], deadline: float
    ) -> _Watch:
        """Spawn one controller life and watch it to a bounded conclusion."""
        try:
            process, stream = await self._spawn(argv, log_path)
        except OSError as exc:
            return _Watch("EXITED", reason=f"spawn failed: {exc}"[:300])
        record["pid"] = process.pid
        waiter: asyncio.Future[int] = asyncio.ensure_future(_wait(process))
        try:
            return await self._watch(record, log_path, waiter, deadline)
        finally:
            await self._kill(process, waiter)
            record["exit_code"] = process.returncode
            with contextlib.suppress(Exception):
                stream.close()

    def _note_death(self, record: dict[str, Any], watch: _Watch, generation: int) -> None:
        """Record STALLED / QUOTA / CRASHED for the controller life that just ended."""
        category = str(record.get("category") or "")
        quota = category in QUOTA_CATEGORIES
        record["state"] = (
            "STALLED" if watch.kind == "STALLED" else ("QUOTA" if quota else "CRASHED")
        )
        self._emit(
            state=record["state"],
            generation=generation,
            category=category,
            exit_code=record["exit_code"],
            detail=watch.reason,
        )

    def _fail_closed(self, reason: str, restarts: int) -> SupervisorOutcome:
        """Last controller life is spent: leave a BLOCKED verdict in the event log."""
        self._emit(state="FAILED_CLOSED", detail=reason[:300])
        if self._finished() is None:
            path = self.run_dir / EVENTS_FILE
            emit_event(path, "run_finished", outcome="BLOCKED", reason=reason[:500])
        return SupervisorOutcome("BLOCKED", reason, restarts, self.generations)


async def _wait(process: SupervisedProcess) -> int:
    return await process.wait()


# --------------------------------------------------------------------- CLI


def build_command_factory(run_id: str, extra: Sequence[str]) -> Callable[[int], list[str]]:
    """``verdict orchestrate <args> --resume <run_id>`` for every controller generation."""
    base = [sys.executable, "-m", "verdict", "orchestrate", *extra, "--resume", run_id]

    def factory(generation: int) -> list[str]:
        del generation  # --resume makes every life reuse already VALIDATED nodes
        return list(base)

    return factory


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``verdict supervise`` (verdict/cli.py wires this in)."""
    parser = subparsers.add_parser("supervise", help="Supervise an orchestration controller")
    parser.add_argument("--run-id", required=True, help="Run id to supervise and resume")
    parser.add_argument("--runs-dir", required=True, help="Directory holding run directories")
    parser.add_argument("--stall-seconds", type=float, default=300.0, help="No-progress limit")
    parser.add_argument("--poll-seconds", type=float, default=5.0, help="Liveness poll interval")
    parser.add_argument("--max-restarts", type=int, default=2, help="Controller restart budget")
    parser.add_argument(
        "--total-deadline-seconds", type=float, default=7200.0, help="Hard deadline"
    )
    parser.add_argument("orchestrate_args", nargs=argparse.REMAINDER, help="Args after `--`")
    parser.set_defaults(func=dispatch)
    result: argparse.ArgumentParser = parser
    return result


def dispatch(args: argparse.Namespace) -> int:
    """Run the supervisor; exit 0 only on COMPLETE (fail closed otherwise)."""
    extra = [str(token) for token in (getattr(args, "orchestrate_args", None) or [])]
    while extra and extra[0] == "--":
        extra.pop(0)
    run_id = str(args.run_id)
    supervisor = ControllerSupervisor(
        build_command_factory(run_id, extra),
        Path(args.runs_dir) / run_id,
        stall_seconds=float(getattr(args, "stall_seconds", 300.0)),
        poll_seconds=float(getattr(args, "poll_seconds", 5.0)),
        max_restarts=int(getattr(args, "max_restarts", 2)),
        total_deadline_seconds=float(getattr(args, "total_deadline_seconds", 7200.0)),
    )
    outcome = asyncio.run(supervisor.run())
    report: dict[str, Any] = {"run_id": run_id, "state": outcome.state, "reason": outcome.reason}
    report.update(restarts=outcome.restarts, generations=outcome.generations)
    print(json.dumps(report, indent=2))
    return 0 if outcome.state == "COMPLETE" else 1
