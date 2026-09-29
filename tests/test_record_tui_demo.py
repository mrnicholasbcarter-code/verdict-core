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
