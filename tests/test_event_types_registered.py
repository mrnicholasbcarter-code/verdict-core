"""Every event type Verdict emits is registered, so the real EventLog accepts it.

Unit tests record events through fake sinks that do not validate types, so an
unregistered type only fails in a real run (the EventLog raises
OrchestrationError). This scans the source for every .emit("<type>", ...) call,
including multi-line ones.
"""

from __future__ import annotations

import ast
from pathlib import Path

from verdict.orchestration.contracts import EVENT_TYPES

ROOT = Path(__file__).resolve().parent.parent / "verdict"


def _emitted_types() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "emit"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.setdefault(node.args[0].value, []).append(
                    f"{path.relative_to(ROOT.parent)}:{node.lineno}"
                )
    return found


def test_every_emitted_event_type_is_registered() -> None:
    emitted = _emitted_types()
    assert {"repack", "rehydrate", "review_attempt"} <= set(emitted)
    unregistered = {t: where for t, where in emitted.items() if t not in EVENT_TYPES}
    assert unregistered == {}


def test_real_event_log_accepts_runtime_event_types(tmp_path: Path) -> None:
    from verdict.orchestration.receipt import EventLog

    log = EventLog(tmp_path / "events.jsonl")
    for event_type in ("repack", "rehydrate", "review_attempt"):
        log.emit(event_type, node_id="a", attempt=1)
    assert [e.type for e in log.read()] == ["repack", "rehydrate", "review_attempt"]
