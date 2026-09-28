"""Replay golden capture shim — seeds MemoryPlane from a static fixture."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

# tests/helpers/ -> parents[1] = tests/ -> fixtures/actions_golden/inputs/
_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "actions_golden"
    / "inputs"
    / "replay-session.json"
)
_SESSION_ID = "session-f3golden-bod275"


def _seed_plane(db_path: str) -> None:
    from verdict.execution_session import ExecutionSession
    from verdict.memory_plane import MemoryPlane

    raw = json.loads(_FIXTURE.read_text())
    plane = MemoryPlane(db_path)
    ExecutionSession.create(
        session_id=_SESSION_ID,
        task_spec=raw["task_spec"],
        steps=[(s["step_id"], s["name"]) for s in raw["steps"]],
        model_id=raw.get("model_id"),
        plane=plane,
    )


def main() -> int:
    import verdict.cli as _cli

    # mkstemp creates the file atomically (no predictable-name race); the store opens it.
    fd, tmp_db = tempfile.mkstemp(suffix=".db", prefix="verdict_replay_golden_")
    os.close(fd)
    os.unlink(tmp_db)  # the memory store creates a fresh SQLite file at this unique path
    _seed_plane(tmp_db)
    try:
        os.environ["VERDICT_MEMORY_DB"] = tmp_db
        sys.argv = ["verdict", "replay", _SESSION_ID, "--json"]
        try:
            _cli.main()
            return 0
        except SystemExit as exc:
            code = exc.code
            return code if isinstance(code, int) else (0 if code is None else 1)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_db)


if __name__ == "__main__":
    sys.exit(main())
