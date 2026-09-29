"""Bounded tests for the gallery capture adapter, not final media generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.render_tui_gallery import FIXTURE_LABEL, load_scenario, snapshot_boundaries
from verdict.orchestration.contracts import RunEvent
from verdict.orchestration.tui import RunView


def sample_events() -> list[RunEvent]:
    """Tiny input fixture: never presented as a production scenario."""
    rows = [
        ("run_started", "", {"run_id": "test-gallery", "goal": "test fixture"}),
        ("node_state", "n1", {"state": "RUNNING", "route_id": "fixture/a"}),
        ("hydrate", "n1", {"prompt_bytes": 100, "budget_bytes": 1000}),
        ("node_state", "n1", {"state": "TERMINAL_FAILURE"}),
        ("failure", "n1", {"category": "rate_limit", "route_id": "fixture/a"}),
        ("cooldown", "n1", {"key": "fixture/a", "scope": "route", "category": "rate_limit"}),
        ("reassign", "n1", {"from_route": "fixture/a", "to_route": "fixture/b"}),
        ("dispatch", "n1", {"route_id": "fixture/b"}),
        ("node_state", "n1", {"state": "VALIDATED"}),
        ("run_finished", "", {"outcome": "COMPLETE", "reason": "test fixture"}),
    ]
    return [
        RunEvent(seq=i + 1, at=f"2026-01-01T00:00:{i:02d}Z", type=kind, node_id=node, data=data)
        for i, (kind, node, data) in enumerate(rows)
    ]


def test_snapshots_are_real_unchanged_prefixes() -> None:
    events = sample_events()
    before = [event.to_dict() for event in events]
    running_end, failure_end = snapshot_boundaries(events)
    running = RunView.from_events(events[:running_end])
    failure = RunView.from_events(events[:failure_end])
    assert running.nodes["n1"].state.value == "RUNNING"
    assert not running.failures
    assert failure.failures and failure.cooldowns and failure.reassignments
    assert events[failure_end - 1].type == "reassign"
    assert not failure.final
    assert [event.to_dict() for event in events] == before


def test_running_snapshot_does_not_include_a_terminal_event() -> None:
    events = sample_events()[:3]
    events += [
        RunEvent(
            seq=4, at="2026-01-01T00:00:04Z", type="terminal", node_id="n1", data={"ok": False}
        ),
        RunEvent(
            seq=5,
            at="2026-01-01T00:00:05Z",
            type="failure",
            node_id="n1",
            data={"category": "rate_limit"},
        ),
    ]
    running_end, _ = snapshot_boundaries(events)
    assert events[running_end - 1].type == "node_state"
    assert all(event.type != "terminal" for event in events[:running_end])


def test_running_snapshot_rejects_stale_concurrency() -> None:
    events = sample_events()[:3]
    events += [
        RunEvent(
            seq=4, at="2026-01-01T00:00:04Z", type="terminal", node_id="n1", data={"ok": False}
        ),
        RunEvent(
            seq=5,
            at="2026-01-01T00:00:05Z",
            type="node_state",
            node_id="n2",
            data={"state": "RUNNING"},
        ),
        RunEvent(
            seq=6,
            at="2026-01-01T00:00:06Z",
            type="failure",
            node_id="n1",
            data={"category": "rate_limit"},
        ),
    ]
    running_end, _ = snapshot_boundaries(events)
    assert running_end == 2
    running = RunView.from_events(events[:running_end])
    assert sum(node.state.value == "RUNNING" for node in running.nodes.values()) == 1


def test_running_snapshot_selects_recorded_concurrency() -> None:
    events = sample_events()[:3]
    events += [
        RunEvent(
            seq=4,
            at="2026-01-01T00:00:04Z",
            type="node_state",
            node_id="n2",
            data={"state": "RUNNING"},
        ),
        RunEvent(
            seq=5,
            at="2026-01-01T00:00:05Z",
            type="failure",
            node_id="n1",
            data={"category": "rate_limit"},
        ),
    ]
    running_end, _ = snapshot_boundaries(events)
    assert running_end == 4
    running = RunView.from_events(events[:running_end])
    assert sum(node.state.value == "RUNNING" for node in running.nodes.values()) == 2


def test_missing_failure_is_not_invented() -> None:
    with pytest.raises(ValueError, match="no recorded failure"):
        snapshot_boundaries(sample_events()[:3])


def test_no_running_worker_is_not_invented() -> None:
    with pytest.raises(ValueError, match="no RUNNING worker"):
        snapshot_boundaries(sample_events()[3:])


def test_load_requires_ordered_complete_evidence(tmp_path: Path) -> None:
    events = sample_events()
    event_file = tmp_path / "events.jsonl"
    event_file.write_text("".join(json.dumps(e.to_dict()) + "\n" for e in events))
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps({"outcome": "COMPLETE"}))
    scenario = load_scenario(tmp_path)
    assert scenario.events == events
    assert scenario.run_dir == tmp_path.resolve()
    receipt_file.write_text(json.dumps({"outcome": "BLOCKED"}))
    with pytest.raises(ValueError, match="both events and receipt"):
        load_scenario(tmp_path)
    event_file.write_text("".join(json.dumps(e.to_dict()) + "\n" for e in reversed(events)))
    with pytest.raises(ValueError, match="strictly increasing"):
        load_scenario(tmp_path)


def native_capture(code: str, *args: str) -> dict:
    """Use a clean renderer process, still inside the parent's vrun scope.

    Repository autouse fixtures override Rich Console.size in the pytest process.
    Do not patch production Rich or Verdict methods to defeat that override.
    """
    env = dict(os.environ)
    for key in ("CI", "NO_COLOR", "VERDICT_PLAIN"):
        env.pop(key, None)
    env["TERM"] = "xterm-256color"
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_text_is_exported_before_svg_clears_recording(tmp_path: Path) -> None:
    result = native_capture(
        """import json, sys
from pathlib import Path
from rich.text import Text
from scripts.render_tui_gallery import capture_console, save_capture
console, mode = capture_console(110)
console.print(Text("gallery sentinel", style="#a78bfa"))
files = save_capture(console, Path(sys.argv[1]), "sentinel", 110)
print(json.dumps({"width": console.width, "color": console.color_system,
    "record": console.record, "mode": [mode.color, mode.unicode, mode.width, mode.animate],
    "cleared": console.export_text() == "", "files": files}))
""",
        str(tmp_path),
    )
    assert result["record"] and result["width"] == 110 and result["color"] == "truecolor"
    assert result["mode"] == [True, True, 110, False]
    assert result["cleared"]
    assert "gallery sentinel" in (tmp_path / result["files"]["txt"]).read_text()
    svg = (tmp_path / result["files"]["svg"]).read_text()
    assert "sentinel" in svg and "#a78bfa" in svg


@pytest.mark.parametrize("screen", ["setup", "doctor"])
def test_real_fixture_renderers_are_labeled_and_bounded(screen: str) -> None:
    result = native_capture(
        """import json, sys
from rich.text import Text
from scripts.render_tui_gallery import (
    capture_console, render_setup_fixture, render_doctor_fixture,
)
console, mode = capture_console(110)
render = render_setup_fixture if sys.argv[1] == "setup" else render_doctor_fixture
console = render(console, mode)
text = console.export_text(clear=False)
print(json.dumps({"text": text, "record": console.record, "color": console.color_system,
    "max_columns": max(Text(line).cell_len for line in text.splitlines())}))
""",
        screen,
    )
    text = result["text"]
    assert FIXTURE_LABEL in text
    assert result["record"] and result["color"] == "truecolor"
    assert 5 < len(text.splitlines()) < 100
    assert result["max_columns"] <= 110
    assert "Repair:" in text or "APPLY [NOT RUN]" in text
