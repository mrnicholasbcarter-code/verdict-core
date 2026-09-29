"""Snapshot and semantic tests for demo and trace screens (BOD-280 AC10).

Covers demo and trace at widths 60/100/200 in:
- plain (NO_COLOR=1)
- motion-disabled (VERDICT_NO_ANIMATION=1)

Reuses the interleaving-insensitive comparison helpers from test_demo_trace.py.
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console

from tests.test_flagship_failover_scenario import _run_scenario
from verdict.design import presentation_mode
from verdict.orchestration.claims import CLAIM_STATUS_VERIFIED, derive_claims
from verdict.orchestration.demo_render import render_claims, render_claims_text
from verdict.orchestration.trace_render import render_trace, render_trace_text
from verdict.orchestration.trace_view import trace_view

GOLDEN_DIR_DEMO = Path(__file__).parent / "fixtures" / "demo_render" / "golden"
GOLDEN_DIR_TRACE = Path(__file__).parent / "fixtures" / "trace_render" / "golden"

# The offline scenario label used in _cmd_demo
_DEMO_LABEL = "OFFLINE SCENARIO"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
    events = [
        json.loads(line)
        for line in (result.run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    receipt = json.loads((result.run_dir / "receipt.json").read_text())
    return result.run_dir, events, receipt


def _normalize_seq(text: str) -> str:
    """Normalise seq numbers so goldens survive parallel-worker reordering."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped and stripped[0].isdigit():
            parts = stripped.split(None, 1)
            if parts and parts[0].isdigit():
                line = (
                    line[: len(line) - len(stripped)]
                    + "NNN  "
                    + (parts[1] if len(parts) > 1 else "")
                )
        if "until=" in line:
            line = re.sub(r"until=\S+", "until=<ts>", line)
        out.append(line)
    return "".join(out)


def _extract_node(line: str) -> str:
    """Return the node column value from a normalised trace line."""
    stripped = line.strip()
    if not stripped:
        return ""
    if stripped.startswith(("trace", "goal", "seq", "---", "steps:")):
        return ""
    if not stripped.startswith("NNN"):
        return ""
    parts = stripped.split()
    if len(parts) < 3:
        return ""
    return parts[2]


# These are the global-order anchors (deterministic regardless of parallel interleaving)
_ANCHOR_KINDS = ("request", "plan", "integrate", "run_finished")


def _extract_anchors(lines: list[str]) -> list[str]:
    """Extract global anchor kinds that must stay in strict order (deterministic)."""
    anchors = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("NNN"):
            continue
        parts = stripped.split()
        if len(parts) >= 2:
            kind = parts[1]
            if kind in _ANCHOR_KINDS or kind == "review":
                anchors.append(stripped)
    return anchors


def _assert_trace_golden(got: str, path: Path, *, width: int) -> None:
    """Interleaving-insensitive golden comparison for trace text."""
    got_norm = _normalize_seq(got)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got_norm)
    if not path.exists():
        pytest.fail(f"golden file missing: {path}  (run UPDATE_GOLDEN=1 pytest to create it)")
    expected_norm = _normalize_seq(path.read_text())

    got_lines = [ln for ln in got_norm.splitlines() if ln.strip()]
    exp_lines = [ln for ln in expected_norm.splitlines() if ln.strip()]

    # (1) Multiset equality
    assert Counter(got_lines) == Counter(exp_lines), (
        f"golden multiset mismatch at width={width}: path={path}"
    )
    # (2) Per-node subsequence order
    got_by_node: dict[str, list[str]] = {}
    exp_by_node: dict[str, list[str]] = {}
    for line in got_lines:
        node = _extract_node(line)
        if node:
            got_by_node.setdefault(node, []).append(line)
    for line in exp_lines:
        node = _extract_node(line)
        if node:
            exp_by_node.setdefault(node, []).append(line)
    for node in exp_by_node:
        assert got_by_node.get(node) == exp_by_node[node], (
            f"golden node-order mismatch for {node} at width={width}"
        )
    # (3) Global anchors in order
    got_anchors = _extract_anchors(got_lines)
    exp_anchors = _extract_anchors(exp_lines)
    assert got_anchors == exp_anchors, f"golden anchor-order mismatch at width={width}"


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


class TestDemoTraceSnapshots:
    """Golden snapshots and semantic checks for demo + trace at 60/100/200."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)
        self.tv = trace_view(self.run_dir)
        self.claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)

    # -----------------------------------------------------------------------
    # Semantic checks: always-true regardless of width/mode
    # -----------------------------------------------------------------------

    def test_offline_label_in_claims_text(self) -> None:
        """render_claims_text includes the OFFLINE SCENARIO label."""
        label = f"{_DEMO_LABEL}: scripted workers, injected faults"
        text = render_claims_text(self.claims, label=label, width=100)
        assert _DEMO_LABEL in text

    def test_all_verified_claims_have_evidence(self) -> None:
        verified = [c for c in self.claims if c.status == CLAIM_STATUS_VERIFIED]
        assert verified, "no VERIFIED claims in offline scenario"
        for claim in verified:
            assert claim.evidence, f"VERIFIED claim {claim.id!r} has no evidence"

    def test_claims_verified_header_present(self) -> None:
        text = render_claims_text(self.claims, width=100)
        assert "CLAIMS VERIFIED" in text

    # -----------------------------------------------------------------------
    # Plain (NO_COLOR) — no ANSI at any width
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_trace_plain_no_ansi(self, width: int) -> None:
        with patch.dict(os.environ, {"NO_COLOR": ""}, clear=True):
            text = render_trace_text(self.tv, width=width)
        assert "\x1b" not in text, f"ANSI found in plain trace at width={width}"

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_demo_plain_no_ansi(self, width: int) -> None:
        label = f"{_DEMO_LABEL}: scripted workers, injected faults"
        with patch.dict(os.environ, {"NO_COLOR": ""}, clear=True):
            text = render_claims_text(self.claims, label=label, width=width)
        assert "\x1b" not in text, f"ANSI found in plain demo at width={width}"

    # -----------------------------------------------------------------------
    # Motion disabled — render_trace_text and render_claims_text are plain-only
    # functions so they are unaffected; check the Rich renderables don't break
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_trace_motion_disabled_renders(self, width: int) -> None:
        """render_trace succeeds with VERDICT_NO_ANIMATION=1 and output fits width."""
        with patch.dict(os.environ, {"VERDICT_NO_ANIMATION": "1"}, clear=True):
            mode = replace(
                presentation_mode(), width=width, color=True, unicode=True, color_system="truecolor"
            )
        renderable = render_trace(self.tv, mode)
        console = Console(
            width=width, force_terminal=True, color_system="truecolor", legacy_windows=False
        )
        console.size = (width, 80)
        with console.capture() as cap:
            console.print(renderable)
        from rich.text import Text

        for i, line in enumerate(cap.get().splitlines()):
            visible = Text.from_ansi(line).cell_len
            assert visible <= width, f"line {i} width {visible} > {width}: {line!r}"

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_demo_motion_disabled_renders(self, width: int) -> None:
        """render_claims succeeds with VERDICT_NO_ANIMATION=1 and output fits width."""
        label = f"{_DEMO_LABEL}: scripted workers, injected faults"
        with patch.dict(os.environ, {"VERDICT_NO_ANIMATION": "1"}, clear=True):
            mode = replace(
                presentation_mode(), width=width, color=True, unicode=True, color_system="truecolor"
            )
        renderable = render_claims(self.claims, mode, label=label)
        console = Console(
            width=width, force_terminal=True, color_system="truecolor", legacy_windows=False
        )
        console.size = (width, 80)
        with console.capture() as cap:
            console.print(renderable)
        from rich.text import Text

        for i, line in enumerate(cap.get().splitlines()):
            visible = Text.from_ansi(line).cell_len
            assert visible <= width, f"line {i} width {visible} > {width}: {line!r}"

    # -----------------------------------------------------------------------
    # Trace goldens at 60 / 100 / 200 (plain; interleaving-insensitive)
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_trace_golden_plain(self, width: int) -> None:
        """Plain trace text matches golden at each width (or seeds the golden)."""
        text = render_trace_text(self.tv, width=width)
        path = GOLDEN_DIR_TRACE / f"trace-plain-{width}.txt"
        _assert_trace_golden(text, path, width=width)

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_trace_lines_fit_width(self, width: int) -> None:
        text = render_trace_text(self.tv, width=width)
        for i, line in enumerate(text.splitlines()):
            assert len(line) <= width, f"line {i} at width={width} exceeds limit: {line!r}"

    # -----------------------------------------------------------------------
    # Demo claims goldens at 60 / 100 / 200 (plain)
    # -----------------------------------------------------------------------

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_demo_claims_golden_plain(self, width: int) -> None:
        """Plain claims text matches golden at each width (or seeds it).

        Evidence refs include event seq numbers that vary across parallel runs.
        We normalise those before comparison.
        """
        label = f"{_DEMO_LABEL}: scripted workers, injected faults"
        text = render_claims_text(self.claims, label=label, width=width)
        GOLDEN_DIR_DEMO.mkdir(parents=True, exist_ok=True)
        path = GOLDEN_DIR_DEMO / f"claims-plain-{width}.txt"
        # Normalise event seq numbers (e.g. "event:42" -> "event:N") so
        # parallel-worker interleaving does not invalidate the golden.
        norm = re.sub(r"\bevent:\d+", "event:N", text)
        if os.environ.get("UPDATE_GOLDEN") == "1":
            path.write_text(norm)
        if not path.exists():
            pytest.fail(f"golden file missing: {path}  (run UPDATE_GOLDEN=1 pytest to create it)")
        expected = path.read_text()
        assert norm == expected, (
            f"demo claims golden mismatch at width={width} (run UPDATE_GOLDEN=1 to regenerate)"
        )

    @pytest.mark.parametrize("width", [60, 100, 200])
    def test_demo_lines_fit_width(self, width: int) -> None:
        label = f"{_DEMO_LABEL}: scripted workers, injected faults"
        text = render_claims_text(self.claims, label=label, width=width)
        for i, line in enumerate(text.splitlines()):
            assert len(line) <= width, f"demo line {i} at width={width} exceeds limit: {line!r}"
