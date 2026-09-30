"""Bounded offline tests for poster selection, cropping, and poster-only rendering."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_demo_svg", ROOT / "scripts/render_demo_svg.py"
)
assert SPEC is not None and SPEC.loader is not None
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


def write_cast(path: Path, rows: list[list[object]]) -> None:
    header = {"width": 110, "height": 72, "idle_time_limit": 1}
    path.write_text("\n".join(json.dumps(row) for row in [header, *rows]) + "\n")


def test_poster_frame_counts_content_rows_not_cursor_reset_or_trailing_blanks(
    tmp_path: Path,
) -> None:
    cast = tmp_path / "poster.cast"
    write_cast(
        cast,
        [
            [0.0, "o", "COMPLETE VALIDATED plain receipt"],
            [
                4.0,
                "o",
                "\x1b[?2026h\r\x1b[2K\x1b[1A┏━┓\r\nCOMPLETE VALIDATED\r\n┗━┛\r\n   \r\n\x1b[?2026l",
            ],
        ],
    )
    assert renderer.poster_frame(cast) == (1000, 3)
    assert renderer.poster_stamp_ms(cast) == 1000


def test_no_complete_redraw_preserves_header_height(tmp_path: Path) -> None:
    cast = tmp_path / "poster.cast"
    write_cast(cast, [[0.0, "o", "running"], [4.0, "o", "still running"]])
    assert renderer.poster_frame(cast) == (1000, 72)


@pytest.mark.parametrize("poster_only", [False, True])
def test_only_posters_use_content_height(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poster_only: bool
) -> None:
    cast = tmp_path / "poster.cast"
    write_cast(cast, [[0.0, "o", "┏━┓\r\nCOMPLETE VALIDATED\r\n┗━┛\r\n"]])
    animated, poster = tmp_path / "animated.svg", tmp_path / "poster.svg"
    animated.write_text("unchanged animation")
    calls: list[tuple[Path, list[str], int | None]] = []

    def fake_svg_term(
        source: Path, out: Path, extra: list[str], *, height: int | None = None
    ) -> None:
        assert source == cast
        calls.append((out, extra, height))
        out.write_text("<svg/>")

    monkeypatch.setattr(renderer.shutil, "which", lambda _: "/fake/npx")
    monkeypatch.setattr(renderer, "_svg_term", fake_svg_term)
    arguments = [str(cast), str(animated), str(poster)]
    if poster_only:
        arguments.append("--poster-only")
    assert renderer.main(arguments) == 0
    assert calls[-1] == (poster, ["--at", "0"], 3)
    if poster_only:
        assert len(calls) == 1
        assert animated.read_text() == "unchanged animation"
    else:
        assert calls[0] == (animated, [], None)
        assert len(calls) == 2


@pytest.mark.parametrize("name", ["demo", "demo-tui"])
def test_committed_complete_frames_are_cropped_to_last_nonblank_row(name: str) -> None:
    """The poster frame is the COMPLETE cockpit that poster_frame() selects, and
    its crop ends exactly at that frame's last non-blank row (the bottom border
    of the final banner), not at the full recording height."""
    cast = ROOT / "docs/assets" / f"{name}.cast"
    at_ms, height = renderer.poster_frame(cast)
    lines = cast.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    assert height < header["height"], "poster was not cropped"
    # Walk the cast with the same compressed clock poster_frame() uses and take
    # the event that ends at at_ms: that is the frame the poster renders.
    idle = float(header.get("idle_time_limit", 1e9))
    prev, clock, frame = 0.0, 0.0, None
    for line in lines[1:]:
        at, kind, data = json.loads(line)
        clock += min(float(at) - prev, idle)
        prev = float(at)
        if kind == "o" and round(clock * 1000) == at_ms and "COMPLETE" in renderer._visible(data):
            frame = renderer._visible(data)
            break
    assert frame is not None, f"no COMPLETE frame at the poster time {at_ms} ms"
    rows = frame.split("\n")
    last = max(i for i, row in enumerate(rows) if row.strip()) + 1
    assert height == last
    assert rows[height - 1].strip().startswith("\u2517"), rows[height - 1]
