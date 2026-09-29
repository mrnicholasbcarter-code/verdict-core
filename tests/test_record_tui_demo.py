"""Capture validation and PTY sizing for the TUI demo recorder.

These tests do not record a run. They check the reject rules and that the
child sees the cast dimensions through the environment and TIOCSWINSZ.
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import termios
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "record_tui_demo.py"


def _load():
    spec = importlib.util.spec_from_file_location("record_tui_demo", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rec = _load()


def test_rejects_nonzero_exit() -> None:
    chunks = [(0.0, b"hello world " * 30), (0.1, rec.COMPLETION_MARKER.encode())]
    with pytest.raises(rec.CaptureError, match="exited 1"):
        rec.validate_capture(chunks, 1)


def test_rejects_traceback() -> None:
    body = b"Traceback (most recent call last):\n  File x\n" + b"x" * 300
    chunks = [(0.0, body), (0.2, rec.COMPLETION_MARKER.encode())]
    with pytest.raises(rec.CaptureError, match="traceback"):
        rec.validate_capture(chunks, 0)


def test_rejects_empty_and_short() -> None:
    with pytest.raises(rec.CaptureError, match="too short"):
        rec.validate_capture([], 0)
    with pytest.raises(rec.CaptureError, match="too short"):
        rec.validate_capture([(0.0, b"tiny"), (0.1, b"still")], 0)


def test_rejects_when_final_state_never_arrives() -> None:
    chunks = [(0.0, b"partial redraw " * 40), (0.4, b"still running " * 20)]
    with pytest.raises(rec.CaptureError, match="never reaches"):
        rec.validate_capture(chunks, 0)


def test_rejects_when_last_frame_lacks_marker() -> None:
    marker = rec.COMPLETION_MARKER.encode()
    chunks = [(0.0, b"pad " * 80 + marker), (1.0, b"redraw without the marker " * 10)]
    with pytest.raises(rec.CaptureError, match="last frame"):
        rec.validate_capture(chunks, 0)


def test_accepts_a_complete_capture() -> None:
    marker = rec.COMPLETION_MARKER.encode()
    chunks = [(0.0, b"frame " * 50), (0.5, b"done\n" + marker + b"\n")]
    rec.validate_capture(chunks, 0)


def test_coalesce_joins_split_escape_and_sync_block_only() -> None:
    split = [(0.0, b"\x1b[38;2;1"), (0.01, b"0;10mhi"), (0.2, b" next")]
    joined = rec.coalesce_atomic(split)
    assert len(joined) == 2
    assert joined[0] == (0.0, b"\x1b[38;2;10;10mhi")
    assert joined[1][1] == b" next"

    block = [(0.0, b"\x1b[?2026h"), (0.01, b"panel"), (0.02, b"\x1b[?2026l"), (0.4, b"after")]
    atomic = rec.coalesce_atomic(block)
    assert len(atomic) == 2
    assert atomic[0][1] == b"\x1b[?2026hpanel\x1b[?2026l"
    assert atomic[1] == (0.4, b"after")


def test_coalesce_does_not_merge_complete_writes() -> None:
    chunks = [(0.0, b"\x1b[2Jfull"), (0.1, b"\x1b[Hnext")]
    assert rec.coalesce_atomic(chunks) == chunks


def test_pty_size_is_applied(tmp_path: Path) -> None:
    script = tmp_path / "size.py"
    script.write_text(
        "import fcntl, json, os, struct, termios\nrows, cols, _, _ = struct.unpack('HHHH', fcntl.ioctl(1, termios.TIOCGWINSZ, b'\\0' * 8))\nprint(json.dumps({'cols': cols, 'rows': rows, 'COLUMNS': os.environ['COLUMNS'], 'LINES': os.environ['LINES']}))\n",
        encoding="utf-8",
    )
    chunks, code = rec.read_pty_events(
        [__import__("sys").executable, str(script)],
        {
            "PATH": os.environ.get("PATH", ""),
            "COLUMNS": "110",
            "LINES": "34",
            "TERM": "xterm-256color",
        },
        rows=34,
        cols=110,
    )
    assert code == 0, chunks
    text = b"".join(data for _, data in chunks).decode()
    payload = json.loads(text.strip().splitlines()[-1])
    assert payload == {"cols": 110, "rows": 34, "COLUMNS": "110", "LINES": "34"}


def test_set_pty_size_uses_tiocswinsz(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_ioctl(fd: int, op: int, arg: bytes) -> None:
        seen["fd"] = fd
        seen["op"] = op
        seen["arg"] = arg

    monkeypatch.setattr(rec, "fcntl_ioctl", fake_ioctl)
    rec.set_pty_size(7, 34, 110)
    assert seen["fd"] == 7
    assert seen["op"] == termios.TIOCSWINSZ
    assert seen["arg"] == struct.pack("HHHH", 34, 110, 0, 0)


def test_scenario_cast_discloses_source_and_speed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = rec.COMPLETION_MARKER.encode()
    chunks = [
        (1.0, b"VERDICT cooldown reassign " + b"frame " * 50),
        (2.0, b"COMPLETE\n" + marker + b"\nevents_digest mismatch"),
        (3.0, b"exit status: 1\n"),
    ]
    calls: list[tuple[list[str], dict[str, str], int, int]] = []

    def fake_read(argv: list[str], env: dict[str, str], *, rows: int, cols: int):
        calls.append((argv, env, rows, cols))
        return chunks, 0

    monkeypatch.setattr(rec, "read_pty_events", fake_read)
    out = tmp_path / "scenario.cast"
    rec.record_scenario(speed=0.1, output=out)
    header = json.loads(out.read_text().splitlines()[0])
    assert "offline scenario, scripted workers, injected faults" in header["title"]
    assert "replay speed 0.1x" in header["title"]
    assert "scripts/record_tui_demo.py --scenario" in header["title"]
    assert calls[0][2:] == (header["height"], header["width"])
    assert calls[0][1]["COLORTERM"] == "truecolor"
    assert not Path(calls[0][1]["HOME"]).exists()


def test_scenario_rejects_failure_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "existing.cast"
    out.write_text("previous valid capture")
    monkeypatch.setattr(rec, "read_pty_events", lambda *a, **kw: ([(0.0, b"failed")], 1))
    with pytest.raises(rec.CaptureError, match="exited 1"):
        rec.record_scenario(output=out)
    assert out.read_text() == "previous valid capture"


def test_scenario_rejects_nonpositive_speed(tmp_path: Path) -> None:
    with pytest.raises(rec.CaptureError, match="positive"):
        rec.record_scenario(speed=0, output=tmp_path / "scenario.cast")


def test_tamper_changes_exactly_one_byte_and_preserves_json(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    original = b'{"goal":"flagship failover test"}\n'
    path.write_bytes(original)
    rec.tamper_events(path)
    modified = path.read_bytes()
    assert len(original) == len(modified)
    assert sum(a != b for a, b in zip(original, modified, strict=True)) == 1
    assert json.loads(modified)["goal"] == "Flagship failover test"


def test_scenario_requires_real_worker_overlap() -> None:
    sequential = [
        {"type": "dispatch", "node_id": "a"},
        {"type": "terminal", "node_id": "a"},
        {"type": "dispatch", "node_id": "b"},
        {"type": "failure"},
        {"type": "cooldown"},
        {"type": "reassign"},
    ]
    with pytest.raises(rec.CaptureError, match="two dispatched"):
        rec.validate_scenario_events(sequential)
    concurrent = [sequential[0], sequential[2], sequential[1], *sequential[3:]]
    rec.validate_scenario_events(concurrent)
    with pytest.raises(rec.CaptureError, match="ordered failure"):
        rec.validate_scenario_events(concurrent[:3])


def test_pty_idle_timeout_rejects_and_reaps_child(tmp_path: Path) -> None:
    script = tmp_path / "silent.py"
    script.write_text("import time; time.sleep(2)\n")
    with pytest.raises(rec.CaptureError, match="PTY produced no output"):
        rec.read_pty_events(
            [__import__("sys").executable, str(script)],
            {"PATH": os.environ.get("PATH", "")},
            rows=34,
            cols=110,
            read_timeout=0.05,
        )


def test_scenario_height_rejects_oversized_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rec, "SCENARIO_HEIGHT", 10)
    with pytest.raises(rec.CaptureError, match="home frame would be clipped"):
        rec.validate_scenario_height([], include_home=True)


def test_scenario_height_bounds_frame_count() -> None:
    with pytest.raises(rec.CaptureError, match="2000-frame"):
        rec.validate_scenario_height([{}] * 2001, include_home=False)


def test_svg_renderer_uses_cast_dimensions_and_generic_font(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location(
        "render_demo_svg", ROOT / "scripts/render_demo_svg.py"
    )
    assert spec is not None and spec.loader is not None
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    cast = tmp_path / "sample.cast"
    cast.write_text(json.dumps({"width": 110, "height": 72}) + "\n")
    out = tmp_path / "sample.svg"
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, check: bool) -> None:
        assert check
        commands.append(command)
        out.write_text("<svg font-family=\"Monaco,'Powerline Symbols',monospace\"/>")

    monkeypatch.setattr(renderer.subprocess, "run", fake_run)
    renderer._svg_term(cast, out, [])
    command = commands[0]
    assert command[command.index("--height") + 1] == "72"
    assert command[command.index("--width") + 1] == "110"
    assert "Powerline" not in out.read_text()
    assert "monospace" in out.read_text()
