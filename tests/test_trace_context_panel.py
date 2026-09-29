"""Tests for trace --panel context (BOD-278 AC6).

AC evidence:
- verdict trace RUN --node N --panel context renders the node's context assembly
- --json returns context_json-compatible payload
- JSON parity with verdict context
- Clear message for a node without context
- Context step lines in text trace include hint commands
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_flagship_failover_scenario import _run_scenario
from verdict.actions.views import _action_context_view
from verdict.orchestration.context_render import context_json, render_context_text
from verdict.orchestration.context_view import context_view
from verdict.orchestration.trace_render import render_trace_text
from verdict.orchestration.trace_view import trace_view

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def run_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the flagship scenario and return (run_dir, trace_view, context_view)."""
    result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
    tv = trace_view(result.run_dir)
    cv = context_view(result.run_dir)
    return result.run_dir, tv, cv


# ---------------------------------------------------------------------------
# Part A: --panel context for a node with context data
# ---------------------------------------------------------------------------


class TestTracePanelContext:
    """verdict trace RUN --node N --panel context opens context assembly."""

    @pytest.fixture(autouse=True)
    def setup(self, run_data: Any) -> None:
        self.run_dir, self.tv, self.cv = run_data

    def test_context_steps_exist(self) -> None:
        """The flagship scenario has context steps."""
        context_steps = self.tv.steps_by_kind("context")
        assert context_steps, "no context steps in flagship trace"

    def test_panel_renders_for_node_with_context(self) -> None:
        """_action_context_view returns a non-empty view for a node that has hydrate events."""
        context_steps = self.tv.steps_by_kind("context")
        node_id = context_steps[0].node_id
        assert node_id, "context step has no node_id"

        result = _action_context_view(
            run=str(self.run_dir), runs_dir=str(self.run_dir.parent), node=node_id
        )
        assert result.ok, f"context panel failed: {result.data.get('error')}"
        view = result.data["view"]
        assert view is not None
        # Must have at least one node matching
        assert any(n.node_id == node_id for n in view.nodes), (
            f"returned view has no node matching {node_id!r}"
        )

    def test_panel_text_render_no_ansi(self) -> None:
        """render_context_text for a node panel produces no ANSI."""
        context_steps = self.tv.steps_by_kind("context")
        node_id = context_steps[0].node_id

        result = _action_context_view(
            run=str(self.run_dir), runs_dir=str(self.run_dir.parent), node=node_id
        )
        assert result.ok
        text = render_context_text(result.data["view"], width=100)
        assert "\x1b" not in text, "context panel text must be ANSI-free"

    def test_panel_json_parity_with_context_command(self) -> None:
        """--panel context --json output matches context_json for the same node.

        One side is exercised through _cmd_trace (capturing stdout); the other
        is the direct context_json call that verdict context --json uses.
        """
        import argparse
        import io
        from contextlib import redirect_stdout

        from verdict.commands.dispatch import _cmd_trace

        context_steps = self.tv.steps_by_kind("context")
        node_id = context_steps[0].node_id

        # Side A: invoke _cmd_trace --panel context --json, capture stdout
        ns = argparse.Namespace(
            run=str(self.run_dir),
            runs_dir=str(self.run_dir.parent),
            panel="context",
            node=node_id,
            json=True,
            step=None,
            kind=None,
            routing=False,
            context=False,
            width=100,
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            _cmd_trace(ns)
        cli_payload = json.loads(buf.getvalue())

        # Side B: direct context_json call (same as verdict context --json)
        direct_result = _action_context_view(
            run=str(self.run_dir), runs_dir=str(self.run_dir.parent), node=node_id
        )
        assert direct_result.ok
        direct_payload = context_json(direct_result.data["view"])

        # Both payloads must be equivalent JSON
        assert json.dumps(cli_payload, sort_keys=True) == json.dumps(
            direct_payload, sort_keys=True
        ), "panel --json diverges from context command JSON"

    def test_panel_json_has_required_keys(self) -> None:
        """Context JSON payload has the required top-level keys."""
        context_steps = self.tv.steps_by_kind("context")
        node_id = context_steps[0].node_id

        result = _action_context_view(
            run=str(self.run_dir), runs_dir=str(self.run_dir.parent), node=node_id
        )
        assert result.ok
        payload = context_json(result.data["view"])
        assert "schema_version" in payload
        assert "nodes" in payload
        assert "aggregate" in payload

    def test_panel_json_node_matches_requested(self) -> None:
        """When --node is specified, the returned payload contains only that node."""
        context_steps = self.tv.steps_by_kind("context")
        node_id = context_steps[0].node_id

        result = _action_context_view(
            run=str(self.run_dir), runs_dir=str(self.run_dir.parent), node=node_id
        )
        assert result.ok
        payload = context_json(result.data["view"])
        node_ids_in_payload = [n["node_id"] for n in payload["nodes"]]
        assert node_ids_in_payload == [node_id], (
            f"payload nodes {node_ids_in_payload!r} != [{node_id!r}]"
        )


# ---------------------------------------------------------------------------
# Part A: clear message for a node without context
# ---------------------------------------------------------------------------


class TestTracePanelNoContext:
    """Requesting context for a node with no hydrate events gives a clear message."""

    @pytest.fixture(autouse=True)
    def setup(self, run_data: Any) -> None:
        self.run_dir, self.tv, self.cv = run_data

    def test_unknown_node_fails_with_clear_message(self) -> None:
        result = _action_context_view(
            run=str(self.run_dir), runs_dir=str(self.run_dir.parent), node="no-such-node-xyz"
        )
        assert not result.ok
        error = result.data.get("error", "")
        assert "no-such-node-xyz" in error or "node" in error.lower(), (
            f"error message does not mention the node: {error!r}"
        )

    def test_render_context_text_for_empty_view_no_ansi(self) -> None:
        """render_context_text on an empty node set must not contain ANSI."""
        from verdict.orchestration.context_render import render_context_text
        from verdict.orchestration.context_view import ContextView

        empty = ContextView(schema_version="context-view-v1", run_id="test", nodes=[])
        text = render_context_text(empty, width=80)
        assert "\x1b" not in text


# ---------------------------------------------------------------------------
# Part A: context step hint lines in text trace
# ---------------------------------------------------------------------------


class TestTraceContextStepHints:
    """Each context step in render_trace_text includes a hint for --panel context."""

    @pytest.fixture(autouse=True)
    def setup(self, run_data: Any) -> None:
        self.run_dir, self.tv, self.cv = run_data

    def test_context_hint_present_in_trace_text(self) -> None:
        text = render_trace_text(self.tv, width=120)
        context_steps = self.tv.steps_by_kind("context")
        assert context_steps, "no context steps to verify"
        # At least one hint line must appear for a context step with a run_id and node_id
        steps_with_node = [s for s in context_steps if s.node_id and self.tv.run_id]
        if not steps_with_node:
            pytest.skip("no context steps with both node_id and run_id")
        # A hint line starts with spaces and contains "--panel context"
        hint_lines = [ln for ln in text.splitlines() if "--panel context" in ln]
        assert hint_lines, "no --panel context hint lines in trace text"

    def test_hint_contains_node_id(self) -> None:
        context_steps = [s for s in self.tv.steps_by_kind("context") if s.node_id]
        if not context_steps or not self.tv.run_id:
            pytest.skip("no context steps with node_id/run_id")
        node_id = context_steps[0].node_id
        text = render_trace_text(self.tv, width=120)
        hint_lines = [ln for ln in text.splitlines() if "--panel context" in ln]
        assert any(node_id in ln for ln in hint_lines), (
            f"no hint line contains node_id {node_id!r}; hints: {hint_lines}"
        )

    def test_hint_lines_fit_width(self) -> None:
        context_steps = [s for s in self.tv.steps_by_kind("context") if s.node_id]
        if not context_steps or not self.tv.run_id:
            pytest.skip("no context steps with node_id/run_id")
        for width in (60, 80, 100):
            text = render_trace_text(self.tv, width=width)
            for i, line in enumerate(text.splitlines()):
                assert len(line) <= width, (
                    f"line {i} at width {width} exceeds limit ({len(line)}): {line!r}"
                )

    def test_no_hints_without_run_id(self) -> None:
        """If run_id is empty, no hint lines should appear (no command to show)."""
        from dataclasses import replace

        tv_no_id = replace(self.tv, run_id="")
        text = render_trace_text(tv_no_id, width=120)
        assert "--panel context" not in text, "hint should not appear when run_id is empty"


# ---------------------------------------------------------------------------
# Part D: CLI enforcement -- --panel requires --node
# ---------------------------------------------------------------------------


class TestTracePanelRequiresNode:
    """--panel without --node must exit with code 2."""

    def test_panel_without_node_exits_2(self, tmp_path: Path) -> None:
        import argparse

        from verdict.commands.dispatch import _cmd_trace

        # Create a minimal run dir so we don't fail on run resolution
        run_dir = tmp_path / "run_stub"
        run_dir.mkdir()
        (run_dir / "events.jsonl").write_text("")

        ns = argparse.Namespace(
            run=str(run_dir),
            runs_dir=str(tmp_path),
            panel="context",
            node=None,  # missing --node
            json=False,
            step=None,
            kind=None,
            routing=False,
            context=False,
            width=100,
        )
        with pytest.raises(SystemExit) as exc_info:
            _cmd_trace(ns)
        assert exc_info.value.code == 2, (
            f"expected exit(2) for --panel without --node, got {exc_info.value.code}"
        )
