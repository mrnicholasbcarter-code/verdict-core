"""Tests for verdict demo + verdict trace (BOD-279).

CI exercises:
- ``verdict demo --json`` trace sequence and evidence-backed claims
- mutation: removing cooldown → cooldown claim not VERIFIED
- goldens for ``verdict trace`` text at 60/100
- JSON output has no ANSI
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from rich.cells import cell_len
from rich.console import Console

from tests.test_flagship_failover_scenario import _run_scenario
from verdict.design import presentation_mode, state_style
from verdict.orchestration.claims import (
    CLAIM_STATUS_NOT_OBSERVED,
    CLAIM_STATUS_VERIFIED,
    Claim,
    derive_claims,
)
from verdict.orchestration.demo_render import render_claims, render_claims_text
from verdict.orchestration.trace_render import _KIND_STATE, render_trace, render_trace_text
from verdict.orchestration.trace_view import STEP_KINDS, trace_view

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "trace_render" / "golden"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claims_by_id(claims: list[Claim]) -> dict[str, Claim]:
    return {c.id: c for c in claims}


def _load_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, list[dict[str, Any]], dict[str, Any]]:
    """Run the flagship scenario and return (run_dir, events, receipt)."""
    result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
    events = [
        json.loads(line)
        for line in (result.run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    receipt = json.loads((result.run_dir / "receipt.json").read_text())
    return result.run_dir, events, receipt


# ---------------------------------------------------------------------------
# CI: trace sequence assertion
# ---------------------------------------------------------------------------


class TestDemoTraceSequence:
    """verdict demo --json trace sequence and evidence-backed claims."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)
        self.tv = trace_view(self.run_dir)
        self.claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)

    def test_trace_sequence_includes_required_kinds(self) -> None:
        """Trace contains failure → cooldown → reassign/selection → terminal → verify → review."""
        kinds = [s.kind for s in self.tv.steps]
        required = [
            "failure",
            "cooldown",
            "selection",
            "terminal",
            "verify",
            "review",
            "run_finished",
        ]
        for req in required:
            assert req in kinds, f"missing required kind: {req}"

    def test_failure_precedes_cooldown(self) -> None:
        kinds = [s.kind for s in self.tv.steps]
        failure_idx = next(i for i, k in enumerate(kinds) if k == "failure")
        cooldown_idx = next(i for i, k in enumerate(kinds) if k == "cooldown")
        assert failure_idx < cooldown_idx, "failure must precede cooldown"

    def test_cooldown_precedes_replacement_terminal(self) -> None:
        """After cooldown, there must be a second selection + terminal."""
        kinds = [s.kind for s in self.tv.steps]
        cooldown_idx = next(i for i, k in enumerate(kinds) if k == "cooldown")
        # Find terminal after cooldown
        post_cooldown = kinds[cooldown_idx:]
        assert "terminal" in post_cooldown, "replacement terminal must follow cooldown"

    def test_verified_claims_have_evidence(self) -> None:
        """Every VERIFIED claim has at least one evidence ref."""
        for claim in self.claims:
            if claim.status == CLAIM_STATUS_VERIFIED:
                assert claim.evidence, f"VERIFIED claim {claim.id!r} has no evidence"

    def test_trace_has_run_finished(self) -> None:
        kinds = [s.kind for s in self.tv.steps]
        assert kinds[-1] == "run_finished"


# ---------------------------------------------------------------------------
# Mutation test: removing cooldown record
# ---------------------------------------------------------------------------


class TestCooldownMutation:
    """Removing the cooldown event changes the cooldown claim status."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)

    def test_cooldown_verified_normally(self) -> None:
        claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        by_id = _claims_by_id(claims)
        assert by_id["cooldown_recorded"].status == CLAIM_STATUS_VERIFIED

    def test_cooldown_not_verified_without_cooldown_event(self) -> None:
        """Remove cooldown events → cooldown claim is no longer VERIFIED."""
        mutated = [e for e in self.events if e.get("type") != "cooldown"]
        assert len(mutated) < len(self.events), "cooldown event should have been present"
        claims = derive_claims(mutated, self.receipt)
        by_id = _claims_by_id(claims)
        assert by_id["cooldown_recorded"].status != CLAIM_STATUS_VERIFIED


# ---------------------------------------------------------------------------
# Goldens: trace text at 60/100
# ---------------------------------------------------------------------------


class TestTraceGoldens:
    """Golden files for trace text rendering at different widths."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)
        self.tv = trace_view(self.run_dir)

    @staticmethod
    def _normalize_seq(text: str) -> str:
        """Normalise seq numbers so goldens survive parallel-worker reordering."""
        lines = text.splitlines(keepends=True)
        out: list[str] = []
        for line in lines:
            # Lines starting with a seq number: replace digits with "NNN"
            stripped = line.lstrip()
            if stripped and stripped[0].isdigit():
                parts = stripped.split(None, 1)
                if parts:
                    line = (
                        line[: len(line) - len(stripped)]
                        + "NNN  "
                        + (parts[1] if len(parts) > 1 else "")
                        + ("\n" if line.endswith("\n") else "")
                    )
            if "until=" in line:
                import re

                line = re.sub(r"until=\S+", "until=<ts>", line)
            out.append(line)
        return "".join(out)

    @staticmethod
    def _extract_node(line: str) -> str:
        """Return the node column value from a normalised trace line."""
        stripped = line.strip()
        if not stripped or stripped.startswith("trace") or stripped.startswith("goal"):
            return ""
        if stripped.startswith("seq") or stripped.startswith("---"):
            return ""
        if stripped.startswith("steps:"):
            return ""
        # Format: NNN  kind            node      detail
        parts = stripped.split()
        if len(parts) >= 3 and parts[0] == "NNN":
            # Check remaining parts for known node names
            for p in parts[2:]:
                if p in ("node-1", "node-2", "integrate"):
                    return p
        return ""

    @staticmethod
    def _extract_anchors(lines: list[str]) -> list[str]:
        """Extract global anchor kinds that must stay in strict order."""
        anchor_kinds = ("request", "plan", "integrate", "run_finished")
        # Also review lines
        anchors: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped or not stripped.startswith("NNN"):
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                kind = parts[1]
                if kind in anchor_kinds or kind == "review":
                    anchors.append(stripped)
        return anchors

    def test_golden_widths(self) -> None:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for width in (60, 100):
            got = render_trace_text(self.tv, width=width)
            path = GOLDEN_DIR / f"trace-{width}.txt"
            got_norm = self._normalize_seq(got)
            if not path.exists() or os.environ.get("UPDATE_GOLDEN") == "1":
                path.write_text(got_norm)
            expected_norm = self._normalize_seq(path.read_text())

            got_lines = [ln for ln in got_norm.splitlines() if ln.strip()]
            exp_lines = [ln for ln in expected_norm.splitlines() if ln.strip()]

            # (1) Multiset of normalised lines must be equal
            from collections import Counter

            assert Counter(got_lines) == Counter(exp_lines), (
                f"golden multiset mismatch at width={width}"
            )

            # (2) Per-node subsequence order matches golden
            got_by_node: dict[str, list[str]] = {}
            exp_by_node: dict[str, list[str]] = {}
            for line in got_lines:
                node = self._extract_node(line)
                if node:
                    got_by_node.setdefault(node, []).append(line)
            for line in exp_lines:
                node = self._extract_node(line)
                if node:
                    exp_by_node.setdefault(node, []).append(line)
            for node in exp_by_node:
                assert got_by_node.get(node) == exp_by_node[node], (
                    f"golden node-order mismatch for {node} at width={width}"
                )

            # (3) Global anchors stay in order
            got_anchors = self._extract_anchors(got_lines)
            exp_anchors = self._extract_anchors(exp_lines)
            assert got_anchors == exp_anchors, f"golden anchor-order mismatch at width={width}"

    def test_lines_fit_width(self) -> None:
        """No rendered line exceeds the requested width."""
        for width in (60, 100):
            text = render_trace_text(self.tv, width=width)
            for i, line in enumerate(text.splitlines()):
                assert len(line) <= width, (
                    f"line {i} exceeds width {width}: {len(line)} chars: {line!r}"
                )


# ---------------------------------------------------------------------------
# JSON has no ANSI
# ---------------------------------------------------------------------------


class TestJsonNoAnsi:
    """JSON output contains no ANSI escape sequences."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)
        self.tv = trace_view(self.run_dir)

    def test_trace_json_no_ansi(self) -> None:
        data = self.tv.to_dict()
        raw = json.dumps(data, indent=2, default=str)
        assert "\x1b[" not in raw, "JSON contains ANSI escape"
        assert "\033[" not in raw, "JSON contains ANSI escape"

    def test_claims_json_no_ansi(self) -> None:
        claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        raw = json.dumps([c.to_dict() for c in claims], indent=2, default=str)
        assert "\x1b[" not in raw, "JSON contains ANSI escape"
        assert "\033[" not in raw, "JSON contains ANSI escape"


# ---------------------------------------------------------------------------
# Claims text render
# ---------------------------------------------------------------------------


class TestClaimsRender:
    """Claims text rendering."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)

    def test_render_claims_text_includes_verified(self) -> None:
        claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        text = render_claims_text(claims, label="TEST LABEL")
        assert "CLAIMS VERIFIED" in text
        assert "TEST LABEL" in text

    def test_render_claims_text_offline_label(self) -> None:
        claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        text = render_claims_text(
            claims, label="OFFLINE SCENARIO: scripted workers, injected faults"
        )
        assert "OFFLINE SCENARIO" in text


class TestOfflineClaimsVerified:
    """The offline flagship must prove review and capability filtering."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)
        self.by_id = _claims_by_id(derive_claims(self.events, self.receipt, run_dir=self.run_dir))

    def test_independent_review_verified(self) -> None:
        claim = self.by_id["independent_review"]
        assert claim.status == CLAIM_STATUS_VERIFIED
        assert claim.evidence[-1].value["shared_families"] == []

    def test_capability_filtering_verified(self) -> None:
        claim = self.by_id["capability_filtering"]
        assert claim.status == CLAIM_STATUS_VERIFIED
        assert claim.evidence


class TestRichWidthAndGlyphs:
    """Truecolor panels stay inside the console width; glyphs are width-1."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.run_dir, self.events, self.receipt = _load_run(tmp_path, monkeypatch)
        self.tv = trace_view(self.run_dir)
        self.claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)

    def test_kind_glyphs_are_design_glyphs_of_width_one(self) -> None:
        for kind in STEP_KINDS:
            state, token = _KIND_STATE[kind]
            style = state_style(state)
            assert cell_len(style.glyph) == 1
            assert cell_len(style.ascii_glyph) == 1
            assert token in {"SUCCESS", "ERROR", "WARNING", "PRIMARY", "SECONDARY"}

    @pytest.mark.parametrize("width", (60, 100))
    def test_rich_lines_fit_width(self, width: int) -> None:
        mode = replace(
            presentation_mode(), width=width, color=True, unicode=True, color_system="truecolor"
        )
        for renderable in (
            render_trace(self.tv, mode),
            render_claims(self.claims, mode, label="OFFLINE"),
        ):
            console = Console(
                width=width, force_terminal=True, color_system="truecolor", legacy_windows=False
            )
            console.size = (width, 40)
            with console.capture() as cap:
                console.print(renderable)
            from rich.text import Text

            for i, line in enumerate(cap.get().splitlines()):
                # cell_len on a raw ANSI line counts SGR resets as cells.
                visible = Text.from_ansi(line).cell_len
                assert visible <= width, f"line {i} width {visible} > {width}: {line!r}"

    def test_trace_detail_rows_are_not_blank(self) -> None:
        text = render_trace_text(self.tv, width=100)
        for needle in ("category=", "key=", "until=", "->", "reviewer=", "PASS"):
            assert needle in text, needle

    def test_not_shown_section_absent_when_all_verified(self) -> None:
        text = render_claims_text(self.claims, width=100)
        if all(c.status != CLAIM_STATUS_NOT_OBSERVED for c in self.claims):
            assert "NOT SHOWN" not in text


def test_failed_terminal_never_renders_as_validated() -> None:
    """A failed attempt's terminal row uses the failure state, not the kind's success state."""
    from verdict.orchestration.trace_render import _step_state
    from verdict.orchestration.trace_view import TraceStep

    ok = TraceStep(seq=1, at="", kind="terminal", node_id="n", evidence={"ok": True})
    bad = TraceStep(seq=2, at="", kind="terminal", node_id="n", evidence={"ok": False})
    rejected = TraceStep(seq=3, at="", kind="review", node_id="", evidence={"status": "FAIL"})
    assert _step_state(ok) == ("validated", "SUCCESS")
    assert _step_state(bad) == ("failed", "ERROR")
    assert _step_state(rejected) == ("failed", "ERROR")


def _intervals(events: list[dict[str, Any]], node_id: str) -> list[tuple[int, int]]:
    """(dispatch seq, terminal seq) pairs for one node, in log order."""
    open_seq: int | None = None
    spans: list[tuple[int, int]] = []
    for event in events:
        if event.get("node_id") != node_id:
            continue
        if event.get("type") == "dispatch":
            open_seq = int(event["seq"])
        elif event.get("type") == "terminal" and open_seq is not None:
            spans.append((open_seq, int(event["seq"])))
            open_seq = None
    return spans


def test_scripted_workers_overlap_when_duration_is_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """node-1 and node-2 are both dispatched before either returns a terminal.

    ``worker_seconds=0.2`` is long enough for the scheduler to admit the second
    worker under max_parallel=2, and short enough that the run stays under ~3 s.
    The default of 0 keeps the other tests instant, so overlap is asserted here.
    """
    import time

    from verdict.orchestration.claims import derive_claims
    from verdict.orchestration.demo_scenario import run_flagship_scenario

    del monkeypatch
    started = time.monotonic()
    result = run_flagship_scenario(
        tmp_path / "runs", workspace_root=tmp_path / "workspace", worker_seconds=0.2
    )
    elapsed = time.monotonic() - started
    assert elapsed < 3.0, f"overlap run took {elapsed:.2f}s"

    node1 = _intervals(result.events, "node-1")
    node2 = _intervals(result.events, "node-2")
    assert node1 and node2
    overlapped = any(a0 < b1 and b0 < a1 for a0, a1 in node1 for b0, b1 in node2)
    assert overlapped, (
        f"expected overlapping dispatch→terminal spans, node-1={node1} node-2={node2}"
    )
    # Same-node failover stays causal even while the sibling is in flight.
    kinds = [
        event["type"]
        for event in result.events
        if event.get("node_id") == "node-1" and event["type"] in {"failure", "cooldown", "reassign"}
    ]
    assert kinds.index("failure") < kinds.index("cooldown") < kinds.index("reassign")

    # duration_seconds from the delayed worker must flow through to the terminal event.
    # ScriptedExecutor._delayed now measures with time.monotonic() and sets it.
    # Fault-injected terminals (no delay) and integration merges (route_id="") are excluded.
    assert {
        e["data"].get("executor_kind")
        for e in result.events
        if e["type"] == "terminal" and e["data"].get("route_id")
    } == {"scripted", "fault-injected"}
    worker_ok_terminals = [
        e
        for e in result.events
        if e["type"] == "terminal"
        and e["data"].get("ok")
        and e["data"].get("route_id")  # excludes integration merge (route_id="")
        and not str(e["data"].get("session_ref", "")).startswith("fault-injected")
    ]
    assert worker_ok_terminals, "no successful worker terminal events"
    durations = [e["data"].get("duration_seconds", 0.0) for e in worker_ok_terminals]
    assert all(d >= 0.2 for d in durations), (
        f"expected all successful worker terminal duration_seconds >= 0.2 "
        f"(worker_seconds=0.2); got {durations}"
    )

    claims = derive_claims(result.events, result.receipt, run_dir=result.run_dir)
    verified = {claim.id for claim in claims if claim.status == CLAIM_STATUS_VERIFIED}
    assert len(verified) == 12


def test_demo_json_verifies_twelve_claims_at_zero_and_overlap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``verdict demo --json`` stays VERIFIED at 0 s, at an overlap, and at 1.5 s.

    Three scripted attempts run at 1.5 s, and the failed attempt returns at
    once, so this one test is a few seconds. It is the duration the recording
    uses, so the claim set is checked there rather than assumed.
    """
    from verdict.cli import main

    for seconds in ("0", "0.2", "1.5"):
        monkeypatch.setattr("sys.argv", ["verdict", "demo", "--json", "--worker-seconds", seconds])
        main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["mode"] == "offline"
        claims = payload["claims"]
        assert len(claims) == 12
        assert {claim["status"] for claim in claims} == {CLAIM_STATUS_VERIFIED}


def test_demo_worker_seconds_defaults_to_1_5() -> None:
    """The recording default is 1.5 s. Tests pass 0 so they stay instant."""
    import argparse

    from verdict.commands.parsers_autodev import register

    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["demo"])
    assert args.worker_seconds == 1.5


def test_demo_header_states_worker_duration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The offline header names the scripted duration the command was given."""
    from verdict.cli import main

    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("sys.argv", ["verdict", "demo", "--worker-seconds", "0"])
    main()
    assert (
        "OFFLINE SCENARIO: scripted workers (0 s each), injected faults" in capsys.readouterr().out
    )

    # 0.2 keeps this test short. The parser default of 1.5 is what the
    # recording uses, and the JSON test runs that duration.
    monkeypatch.setattr("sys.argv", ["verdict", "demo", "--worker-seconds", "0.2"])
    main()
    text = capsys.readouterr().out
    assert "OFFLINE SCENARIO: scripted workers (0.2 s each), injected faults" in text
