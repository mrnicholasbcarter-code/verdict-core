"""Supervisor admission pipeline — feature-flagged multi-story governor (BOD-157).

When VERDICT_MULTI_STORY=on, the admission pipeline runs:
  1. ready_decision() — READY / EXCLUDED / WAIT
  2. collide() against every running story — PARALLEL / SERIALIZE
  3. governor.admit() — ADMIT / DEFER / SERIALIZE

Cross-process visibility uses fcntl file locks under the supervisor's state
directory.  Per-story lock files let separate supervisor processes see each
other's running set.  An integration lock file serialises the merge/rebase
step across all processes.

When the flag is off (default) or any unrecognised value, this module is inert:
the supervisor behaves identically to the single-story flock path on origin/main.

All defaults are *provisional pending operator decision* (ADR-037 §Open decisions).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verdict.orchestration.concurrency_governor import (
    AdmitRequest,
    Decision,
    GlobalConcurrencyGovernor,
    GovernorCaps,
    GovernorResult,
    ResourcePressure,
    RunState,
)
from verdict.orchestration.ready_gate import (
    DepEvidence,
    ReadyDecision,
    ReadyVerdict,
    ready_decision,
)
from verdict.orchestration.story_footprint import CollisionVerdict, StoryFootprintV1, collide

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provisional defaults (ADR-037 §Open decisions — most conservative)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SupervisorGovernorConfig:
    """Provisional defaults.  Every value here is pending operator decision.

    Attributes
    ----------
    max_stories : int
        Maximum concurrent stories.  Conservative: 2.
    max_coding_workers : int
        Maximum coding-worker slots across all stories.  Conservative: 2.
    max_integration_slots : int
        Integration (merge/rebase) is always serialised.  Fixed: 1.
    """

    max_stories: int = 2
    max_coding_workers: int = 2
    max_integration_slots: int = 1


DEFAULT_CONFIG = SupervisorGovernorConfig()


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------

_MULTI_STORY_ENV = "VERDICT_MULTI_STORY"


def multi_story_enabled() -> bool:
    """Return True only when the flag is explicitly ``"on"`` (case-insensitive).

    Any other value (including absent, empty, ``"off"``, ``"yes"``, ``"1"``)
    returns False and logs a warning for unrecognised non-empty values.
    """
    raw = os.environ.get(_MULTI_STORY_ENV, "").strip().lower()
    if raw == "on":
        return True
    if raw and raw != "off":
        log.warning(
            "VERDICT_MULTI_STORY=%r is not a recognised value (expected 'on' or 'off'); "
            "treating as OFF",
            os.environ.get(_MULTI_STORY_ENV, ""),
        )
    return False


# ---------------------------------------------------------------------------
# Cross-process file-lock state (used only when flag is on)
# ---------------------------------------------------------------------------

_STORY_LOCKS_DIR = "story-locks"
_INTEGRATION_LOCK_FILE = "integration.lock"


def _locks_dir(state_dir: Path) -> Path:
    """Directory holding per-story lock files."""
    return state_dir / _STORY_LOCKS_DIR


def _story_lock_path(state_dir: Path, story_id: str) -> Path:
    """Path for a per-story lock file.  story_id is sanitised to a safe filename."""
    safe = story_id.replace("/", "_").replace("\\", "_").replace("\0", "_")
    return _locks_dir(state_dir) / f"{safe}.lock"


def _integration_lock_path(state_dir: Path) -> Path:
    """Path for the global integration lock file."""
    return state_dir / _INTEGRATION_LOCK_FILE


@dataclass
class StoryLockHandle:
    """A held per-story fcntl lock.  Call release() or use as context manager."""

    story_id: str
    path: Path
    _fd: int | None = field(default=None, repr=False)

    def release(self) -> None:
        """Release the lock and clean up the lock file."""
        fd = self._fd
        if fd is not None:
            self._fd = None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def __enter__(self) -> StoryLockHandle:
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()

    def __del__(self) -> None:
        self.release()


def acquire_story_lock(
    state_dir: Path, story_id: str, *, block: bool = False
) -> StoryLockHandle | None:
    """Acquire a per-story fcntl file lock.

    Returns a StoryLockHandle on success, or None if the lock is held by
    another process and *block* is False.  When *block* is True, waits
    until the lock is available.

    The lock is released when the handle is released, garbage-collected,
    or the process exits (including crashes — the OS releases fcntl locks
    on fd close / process death).
    """
    path = _story_lock_path(state_dir, story_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write story metadata into the lock file so readers can discover footprints.
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if block:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    handle = StoryLockHandle(story_id=story_id, path=path, _fd=fd)
    return handle


def write_story_metadata(
    handle: StoryLockHandle, footprint: StoryFootprintV1, labels: frozenset[str] = frozenset()
) -> None:
    """Write story metadata into the lock file so other processes can read it."""
    if handle._fd is None:
        return
    data = json.dumps(
        {
            "story_id": handle.story_id,
            "write_paths": sorted(footprint.write_paths),
            "authorities": sorted(footprint.authorities),
            "labels": sorted(labels),
            "pid": os.getpid(),
            "acquired_at": time.time(),
        }
    )
    # Truncate and rewrite from the start.
    os.lseek(handle._fd, 0, os.SEEK_SET)
    os.ftruncate(handle._fd, 0)
    os.write(handle._fd, data.encode())
    os.fsync(handle._fd)


def read_running_stories(state_dir: Path) -> list[RunningStory]:
    """Read the running story set from per-story lock files.

    Tries a non-blocking LOCK_EX on each lock file.  If it fails, the file
    is held by another process → that story is running.  If it succeeds,
    the lock file is stale (process died) → skip it.

    This is the cross-process visibility mechanism: separate supervisor
    processes see each other through the file-lock state.
    """
    locks = _locks_dir(state_dir)
    if not locks.is_dir():
        return []
    running: list[RunningStory] = []
    for lock_path in sorted(locks.glob("*.lock")):
        fd = -1
        try:
            fd = os.open(str(lock_path), os.O_RDONLY)
            # Try non-blocking exclusive lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Got the lock → file is stale (holder died).  Release and skip.
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            fd = -1
            continue
        except BlockingIOError:
            # Lock held by another process → story is running.
            pass
        except OSError:
            if fd >= 0:
                os.close(fd)
            continue
        # Read metadata from the lock file.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 65536)
            os.close(fd)
            fd = -1
            if raw:
                meta = json.loads(raw)
                story_id = str(meta.get("story_id", lock_path.stem))
                wp = frozenset(str(p) for p in (meta.get("write_paths") or []))
                auths = frozenset(str(a) for a in (meta.get("authorities") or []))
                running.append(
                    RunningStory(
                        story_id=story_id,
                        footprint=StoryFootprintV1(
                            story_id=story_id, write_paths=wp, authorities=auths
                        ),
                    )
                )
            else:
                # Empty metadata — treat as unknown footprint (fail-closed).
                sid = lock_path.stem
                running.append(RunningStory(story_id=sid, footprint=StoryFootprintV1(story_id=sid)))
        except (OSError, ValueError, json.JSONDecodeError):
            if fd >= 0:
                os.close(fd)
            # Cannot read metadata — treat as unknown footprint.
            sid = lock_path.stem
            running.append(RunningStory(story_id=sid, footprint=StoryFootprintV1(story_id=sid)))
    return running


@contextmanager
def acquire_integration_lock(state_dir: Path) -> Iterator[None]:
    """Acquire the global integration lock (fcntl file lock).

    Blocks until the lock is available.  Released on exit, including
    on exceptions.  Only ONE process may hold this at a time — this
    serialises the merge/rebase-to-main step.

    The OS releases the lock if the process crashes (fcntl semantics).
    """
    path = _integration_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking
        # Write holder info for debugging.
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        info = json.dumps({"pid": os.getpid(), "acquired_at": time.time()})
        os.write(fd, info.encode())
        os.fsync(fd)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Running-story bookkeeping (legacy in-process + cross-process)
# ---------------------------------------------------------------------------


@dataclass
class RunningStory:
    """Bookkeeping for a concurrently running story."""

    story_id: str
    footprint: StoryFootprintV1


@dataclass
class AdmissionState:
    """Cross-process state tracking running stories for the governor pipeline.

    When a state_dir is provided, running stories are discovered via
    per-story file locks (cross-process visible).  When no state_dir
    is provided, falls back to in-memory tracking (single-process only,
    for tests that do not need cross-process visibility).
    """

    state_dir: Path | None = None
    _running: dict[str, RunningStory] = field(default_factory=dict)

    # -- queries -----------------------------------------------------------

    @property
    def running_stories(self) -> list[RunningStory]:
        """Read the running set.

        When state_dir is set, reads from file locks (cross-process).
        Otherwise returns the in-memory set.
        """
        if self.state_dir is not None:
            return read_running_stories(self.state_dir)
        return list(self._running.values())

    @property
    def running_count(self) -> int:
        return len(self.running_stories)

    # -- in-memory mutations (backward compat for tests) -------------------

    def add(self, story: RunningStory) -> None:
        self._running[story.story_id] = story

    def remove(self, story_id: str) -> None:
        self._running.pop(story_id, None)


# ---------------------------------------------------------------------------
# Admission decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdmissionDecision:
    """Result of :func:`evaluate_admission`."""

    admit: bool
    reason_code: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "admit": self.admit,
            "reason_code": self.reason_code,
            "reason": self.reason,
            **self.detail,
        }


def evaluate_admission(
    *,
    story_id: str,
    labels: frozenset[str],
    deps: list[DepEvidence],
    main_sha: str,
    footprint: StoryFootprintV1,
    admission_state: AdmissionState,
    config: SupervisorGovernorConfig | None = None,
    pressure: ResourcePressure | None = None,
) -> AdmissionDecision:
    """Run the full ADR-037 admission pipeline for *story_id*.

    Returns an :class:`AdmissionDecision` with *admit=True* only when all
    three gates pass.  Fail-closed on any unknown/missing evidence.
    """
    cfg = config or DEFAULT_CONFIG
    pressure = pressure or ResourcePressure()  # unknown → 0.0 per-dim; governor NaN→1.0

    # --- Gate 1: ready_decision -------------------------------------------
    rd: ReadyDecision = ready_decision(labels=labels, deps=deps, main_sha=main_sha)
    if rd.verdict == ReadyVerdict.EXCLUDED:
        return AdmissionDecision(admit=False, reason_code="EXCLUDED", reason=rd.reason)
    if rd.verdict == ReadyVerdict.WAIT:
        return AdmissionDecision(admit=False, reason_code="WAIT", reason=rd.reason)

    # --- Gate 2: collision detection --------------------------------------
    running = admission_state.running_stories
    for rs in running:
        verdict, col_reason = collide(footprint, rs.footprint)
        if verdict == CollisionVerdict.SERIALIZE:
            return AdmissionDecision(
                admit=False,
                reason_code="SERIALIZE",
                reason=f"collision with running story {rs.story_id!r}: {col_reason.value}",
                detail={"collides_with": rs.story_id, "collision_reason": col_reason.value},
            )

    # --- Gate 3: governor -------------------------------------------------
    caps = GovernorCaps(
        max_stories=cfg.max_stories,
        max_coding_workers=cfg.max_coding_workers,
        max_integration_slots=cfg.max_integration_slots,
    )
    governor = GlobalConcurrencyGovernor(caps=caps)
    run_state = RunState(
        running_stories=len(running),
        running_coding_workers=len(running),  # conservative: 1 worker per story
        running_integration_slots=0,
        story_is_already_running=any(rs.story_id == story_id for rs in running),
    )
    request = AdmitRequest(
        story_id=story_id, needs_coding_worker=True, needs_integration_slot=False
    )
    result: GovernorResult = governor.admit(request, run_state, pressure)

    if result.decision == Decision.ADMIT:
        return AdmissionDecision(admit=True, reason_code="ADMIT", reason=result.reason)
    return AdmissionDecision(admit=False, reason_code=result.decision.value, reason=result.reason)
