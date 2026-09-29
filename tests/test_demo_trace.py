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
from pathlib import Path
from typing import Any

import pytest

from tests.test_flagship_failover_scenario import _run_scenario
from verdict.orchestration.claims import CLAIM_STATUS_VERIFIED, Claim, derive_claims
from verdict.orchestration.demo_render import render_claims_text
from verdict.orchestration.trace_render import render_trace_text
from verdict.orchestration.trace_view import trace_view

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
            out.append(line)
        return "".join(out)

    def test_golden_widths(self) -> None:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        for width in (60, 100):
            got = render_trace_text(self.tv, width=width)
            path = GOLDEN_DIR / f"trace-{width}.txt"
            got_norm = self._normalize_seq(got)
            if not path.exists() or os.environ.get("UPDATE_GOLDEN") == "1":
                path.write_text(got_norm)
            expected_norm = self._normalize_seq(path.read_text())
            assert got_norm == expected_norm, f"golden mismatch at width={width}"

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
