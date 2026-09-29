"""tests/test_context_render.py — BOD-278 lane 2: context budget/provenance renderer."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from verdict.design import PresentationMode
from verdict.orchestration.context_render import (
    abbreviate_home,
    context_json,
    format_bytes,
    pressure_band,
    render_context,
    render_context_text,
)
from verdict.orchestration.context_view import ContextView, context_view
from verdict.orchestration.contracts import RunEvent

FIXTURES = Path(__file__).parent / "fixtures" / "context_render"
GOLDEN_DIR = FIXTURES / "golden"


_SEQ = 0


def _seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _ev(type: str, node_id: str = "n1", **data: Any) -> RunEvent:
    return RunEvent(seq=_seq(), at="2026-09-28T14:00:00Z", type=type, node_id=node_id, data=data)


def _src(
    path: str,
    size: int | None = 1000,
    *,
    included: bool = True,
    truncated_at: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "path": path,
        "bytes": size,
        "included": included,
        "truncated_at": truncated_at,
        "reason": reason,
    }


def _hydrate(
    node_id: str = "n1",
    budget_bytes: int | None = 60_000,
    prompt_bytes: int | None = 10_000,
    sources: list[dict[str, Any]] | None = None,
) -> RunEvent:
    data: dict[str, Any] = {"context_files": [], "truncated": False}
    if budget_bytes is not None:
        data["budget_bytes"] = budget_bytes
    if prompt_bytes is not None:
        data["prompt_bytes"] = prompt_bytes
    if sources is not None:
        data["sources"] = sources
    return _ev("hydrate", node_id=node_id, **data)


def _full_view() -> ContextView:
    sources = [
        _src("README.md", 1200, included=True),
        _src("verdict/orchestration/context_view.py", 8000, included=True, truncated_at=4000),
        _src("dup.md", 500, included=False, reason="deduplicated"),
        _src("big.md", 9000, included=False, reason="compressed"),
        _src("skip.md", 100, included=False, reason="budget_exhausted"),
        _src("/home/nick/.config/secret-notes.md", 50, included=True),
        _src("creds/sk-abcdefghijklmnopqrstuvwxyz012345", 20, included=False, reason="unreadable"),
        _src("env/OMNIROUTE_API_KEY=supersecretvalue", 10, included=False, reason="excluded"),
        _src("mystery.bin", None, included=True),  # missing bytes
    ]
    return context_view(
        [
            _ev("run_started", node_id="", run_id="run-abc"),
            _hydrate("alpha", 60_000, 48_000, sources),
            _hydrate("beta", 60_000, 70_000, [_src("only.md", 200)]),
        ]
    )


def _unknown_metrics_view() -> ContextView:
    return context_view([_ev("hydrate", node_id="legacy", context_files=["a.py"], truncated=False)])


# ---------------------------------------------------------------------------
# Pressure / bytes helpers
# ---------------------------------------------------------------------------


class TestPressureAndBytes:
    def test_under_budget_band(self) -> None:
        assert pressure_band(0.5) == "ok"
        assert pressure_band(0.69) == "ok"

    def test_elevated_band(self) -> None:
        assert pressure_band(0.70) == "elevated"
        assert pressure_band(0.85) == "elevated"

    def test_over_band(self) -> None:
        assert pressure_band(1.25) == "over"

    def test_unknown_band(self) -> None:
        assert pressure_band(None) == "unknown"

    def test_format_bytes_unknown(self) -> None:
        assert format_bytes(None) == "unknown"

    def test_format_bytes_kb(self) -> None:
        assert format_bytes(1500) == "1.5 KB"


# ---------------------------------------------------------------------------
# Budget over / under / unknown renders
# ---------------------------------------------------------------------------


class TestBudgetRender:
    def test_under_budget_text(self) -> None:
        view = context_view([_hydrate("n1", 100_000, 20_000, [_src("a.md", 100)])])
        text = render_context_text(view, width=100)
        assert "pressure 20% (ok)" in text
        assert "20.0 KB / 100.0 KB" in text

    def test_over_budget_text(self) -> None:
        view = context_view([_hydrate("n1", 50_000, 80_000, [_src("a.md", 100)])])
        text = render_context_text(view, width=100)
        assert "pressure 160% (over)" in text

    def test_unknown_budget_text(self) -> None:
        view = _unknown_metrics_view()
        text = render_context_text(view, width=100)
        assert "pressure unknown (unknown)" in text
        assert "used unknown / budget unknown" in text
        assert "sources: unknown (not recorded)" in text


# ---------------------------------------------------------------------------
# Truncation + missing optional metrics
# ---------------------------------------------------------------------------


class TestTruncationAndMissing:
    def test_trimmed_state_visible(self) -> None:
        view = context_view(
            [_hydrate(sources=[_src("big.py", 9000, included=True, truncated_at=2048)])]
        )
        text = render_context_text(view, width=120)
        assert "trimmed" in text
        assert "truncated_at=2048" in text

    def test_missing_source_bytes_shows_unknown(self) -> None:
        view = context_view([_hydrate(sources=[_src("x.md", None)])])
        text = render_context_text(view, width=100)
        assert "unknown" in text
        # state still included
        assert "included" in text

    def test_narrow_truncates_long_path(self) -> None:
        long = "a/" * 80 + "end.py"
        view = context_view([_hydrate(sources=[_src(long, 10)])])
        text = render_context_text(view, width=60)
        assert "…" in text
        assert len(text.splitlines()[-1]) <= 60


# ---------------------------------------------------------------------------
# Provenance + redaction
# ---------------------------------------------------------------------------


class TestProvenanceAndRedaction:
    def test_home_abbreviated(self) -> None:
        assert abbreviate_home("/home/nick/dev/x", home="/home/nick") == "~/dev/x"

    def test_secret_path_never_printed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/nick")
        view = _full_view()
        text = render_context_text(view, width=200)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text
        assert "supersecretvalue" not in text
        assert "OMNIROUTE_API_KEY=supersecretvalue" not in text
        # redaction marker present
        assert "[redacted]" in text
        # HOME abbreviated
        assert "/home/nick/" not in text
        assert "~/.config/secret-notes.md" in text

    def test_json_also_redacts(self) -> None:
        payload = context_json(_full_view())
        blob = str(payload)
        assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in blob
        assert "supersecretvalue" not in blob
        assert payload["aggregate"]["pressure_band"] in {"ok", "elevated", "over", "unknown"}

    def test_no_content_bytes_leaked(self) -> None:
        # SourceEntry has no content field; renderer must not invent one
        view = _full_view()
        text = render_context_text(view, width=200)
        assert "content=" not in text
        assert "payload=" not in text


# ---------------------------------------------------------------------------
# Distinguishable source states
# ---------------------------------------------------------------------------


class TestSourceStateLabels:
    def test_all_states_have_distinct_glyphs_and_labels(self) -> None:
        sources = [
            _src("inc.md", 1, included=True),
            _src("trim.md", 2, included=True, truncated_at=1),
            _src("exc.md", 3, included=False, reason="budget_exhausted"),
            _src("ded.md", 4, included=False, reason="deduplicated"),
            _src("cmp.md", 5, included=False, reason="compressed"),
            {"path": "unk.md", "bytes": 6},  # no included flag -> unknown
        ]
        text = render_context_text(context_view([_hydrate(sources=sources)]), width=120)
        for label in ("included", "trimmed", "excluded", "deduplicated", "compressed", "unknown"):
            assert label in text
        # ascii glyphs from the map
        for glyph in ("*", "T", "o", "=", "v", "?"):
            assert glyph in text


# ---------------------------------------------------------------------------
# Replay consistency
# ---------------------------------------------------------------------------


class TestReplayConsistency:
    def test_same_events_same_text(self) -> None:
        events = [
            _ev("run_started", node_id="", run_id="r1"),
            _hydrate("n1", 60_000, 10_000, [_src("a.md", 100), _src("b.md", 200, included=False)]),
        ]
        a = render_context_text(context_view(events), width=100)
        b = render_context_text(context_view(events), width=100)
        assert a == b

    def test_same_events_same_json(self) -> None:
        view = _full_view()
        assert context_json(view) == context_json(view)

    def test_proof_dir_replay_stable(self) -> None:
        proof = Path("docs/proof/live-controller-run")
        if not proof.exists():
            pytest.skip("proof fixture missing")
        v1 = context_view(proof)
        t1 = render_context_text(v1, width=100)
        t2 = render_context_text(context_view(proof), width=100)
        assert t1 == t2
        assert "sources: unknown (not recorded)" in t1


# ---------------------------------------------------------------------------
# Presentation modes / goldens
# ---------------------------------------------------------------------------


class TestPresentationAndGoldens:
    def test_plain_no_ansi(self) -> None:
        text = render_context_text(_full_view(), width=100)
        assert "\x1b[" not in text

    def test_golden_widths(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/nick")
        view = _full_view()
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for width in (60, 100, 200):
            got = render_context_text(view, width=width)
            path = GOLDEN_DIR / f"context-{width}.txt"
            if not path.exists() or os.environ.get("UPDATE_GOLDEN") == "1":
                path.write_text(got)
            assert got == path.read_text(), f"golden mismatch at width={width}"

    def test_no_line_exceeds_width(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", "/home/nick")
        view = _full_view()
        for width in (60, 100, 200):
            text = render_context_text(view, width=width)
            for i, line in enumerate(text.splitlines()):
                assert len(line) <= width, f"width={width} line {i} len={len(line)}: {line!r}"

    def test_source_glyphs_are_width_one(self) -> None:
        from rich.cells import cell_len

        from verdict.orchestration.context_render import _SOURCE_STYLE

        for state, (_label, glyph, ascii_glyph, _token) in _SOURCE_STYLE.items():
            assert cell_len(glyph) == 1, f"{state} unicode glyph {glyph!r}"
            assert cell_len(ascii_glyph) == 1, f"{state} ascii glyph {ascii_glyph!r}"

    def test_colour_uses_design_tokens_only(self) -> None:
        import re

        mode = PresentationMode(
            color=True, unicode=True, animate=False, width=100, color_system="truecolor"
        )
        console = Console(record=True, width=100, force_terminal=True, color_system="truecolor")
        console.print(render_context(_full_view(), mode))
        plain_export = console.export_text()
        src = Path("verdict/orchestration/context_render.py").read_text()
        # No hard-coded #rrggbb / colour-name styles outside design.py
        # Hex colour literals only (ignore PR refs like #708)
        assert re.search(r"#[0-9a-fA-F]{6}\b", src) is None
        for banned in ("bright_red", "bright_green", "magenta", "cyan1", "rgb("):
            assert banned not in src
        assert "token_style(" in src
        assert "from verdict.design import" in src
        assert "budget" in plain_export.lower()
        # State colouring only: row must not be a single whole-line coloured Text dump
        assert "Table(" in src
        assert "box=None" in src


# ---------------------------------------------------------------------------
# Cockpit wiring smoke
# ---------------------------------------------------------------------------


class TestCockpitWire:
    def test_open_context_view_toggles(self) -> None:
        from verdict.orchestration.cockpit_nav import CockpitState, open_context_view

        class _Node:
            prompt_bytes = 1000
            budget_bytes = 60_000

        class _View:
            def __init__(self) -> None:
                self.nodes = {"n1": _Node()}

        state = CockpitState()
        open_context_view(state, _View())
        assert state.context_open is True
        assert state.context_view is not None
        text = state.context_render_text(state.context_view, width=80)
        assert "budget" in text
        open_context_view(state, _View())
        assert state.context_open is False


def test_budget_bar_fills_render_width_and_never_overflows() -> None:
    """The colour budget bar is sized by Rich at render time (not a fixed 30 cells)."""
    import io
    from dataclasses import replace

    from rich.cells import cell_len
    from rich.text import Text

    from verdict.design import presentation_mode

    view = _full_view()
    widths = {}
    for w in (60, 100, 200):
        buf = io.StringIO()
        con = Console(file=buf, width=w, force_terminal=True, color_system="truecolor")
        con.size = (w, 40)  # pin the render width; do not inherit the test runner's terminal
        mode = replace(
            presentation_mode(), width=w, color=True, unicode=True, color_system="truecolor"
        )
        con.print(render_context(view, mode))
        lines = Text.from_ansi(buf.getvalue()).plain.splitlines()
        assert max(cell_len(line) for line in lines) <= w
        widths[w] = max(line.count("━") for line in lines)
    # 60 cols puts the bar on its own row (widest), then it grows with the terminal.
    assert widths[100] < widths[200]
    assert all(v >= 10 for v in widths.values()), widths
