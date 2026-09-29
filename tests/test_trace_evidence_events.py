"""Tests for BOD-277/278/279 trace evidence fields in orchestration events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import CapacityClass, EligibilityStage, RouteVerdict
from verdict.orchestration.runtime import (
    _MAX_CANDIDATES,
    _build_candidates,
    _build_rejections,
    _hydrate_sources,
    _ladder_counts,
)

# ---------------------------------------------------------------------------
# 1. _build_rejections
# ---------------------------------------------------------------------------


class TestBuildRejections:
    def test_empty(self) -> None:
        assert _build_rejections([]) == {}

    def test_aggregates_by_stage_and_reason(self) -> None:
        vs = [
            RouteVerdict(
                "a/m1", "a", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown"
            ),
            RouteVerdict(
                "a/m2", "a", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown"
            ),
            RouteVerdict(
                "b/m1", "b", EligibilityStage.ENTITLED, EligibilityStage.HEALTHY, "probe_failed"
            ),
        ]
        rej = _build_rejections(vs)
        assert rej == {"AVAILABLE": {"cooldown": 2}, "HEALTHY": {"probe_failed": 1}}

    def test_selected_routes_not_rejected(self) -> None:
        vs = [RouteVerdict("a/m1", "a", EligibilityStage.SELECTED, None, "ok")]
        assert _build_rejections(vs) == {}


# ---------------------------------------------------------------------------
# 2. _build_candidates
# ---------------------------------------------------------------------------


class TestBuildCandidates:
    def test_empty(self) -> None:
        cands, omitted, summary = _build_candidates([], None)
        assert cands == []
        assert omitted == 0
        assert summary is None

    def test_selected_always_first(self) -> None:
        vs = [
            RouteVerdict(
                "a/m1", "a", EligibilityStage.AVAILABLE, EligibilityStage.TASK_ELIGIBLE, "excluded"
            ),
            RouteVerdict("b/m1", "b", EligibilityStage.SELECTED, None, "ok", rank=1),
        ]
        cands, omitted, summary = _build_candidates(vs, "b/m1")
        assert cands[0]["route_id"] == "b/m1"
        assert omitted == 0
        assert summary is None

    def test_cap_at_max_candidates(self) -> None:
        vs = [
            RouteVerdict(
                f"p/m{i}", "p", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown"
            )
            for i in range(_MAX_CANDIDATES + 10)
        ]
        cands, omitted, summary = _build_candidates(vs, None)
        assert len(cands) == _MAX_CANDIDATES
        assert omitted == 10
        assert summary == {"cooldown": {"count": 10, "first_reason": "cooldown"}}

    def test_selected_included_when_over_cap(self) -> None:
        vs = [
            RouteVerdict(
                f"p/m{i}", "p", EligibilityStage.HEALTHY, EligibilityStage.AVAILABLE, "cooldown"
            )
            for i in range(_MAX_CANDIDATES + 10)
        ]
        sel = RouteVerdict("sel/best", "sel", EligibilityStage.SELECTED, None, "ok", rank=1)
        vs.append(sel)
        cands, omitted, summary = _build_candidates(vs, "sel/best")
        assert len(cands) == _MAX_CANDIDATES
        assert cands[0]["route_id"] == "sel/best"
        assert omitted == 11
        assert summary == {"cooldown": {"count": 11, "first_reason": "cooldown"}}

    def test_to_dict_fields_present(self) -> None:
        v = RouteVerdict(
            "a/m1",
            "a",
            EligibilityStage.SELECTED,
            None,
            "ok",
            CapacityClass.SUBSCRIPTION,
            plan_label="pro",
            rank=1,
        )
        cands, _, _ = _build_candidates([v], "a/m1")
        d = cands[0]
        assert d["route_id"] == "a/m1"
        assert d["provider"] == "a"
        assert d["reached"] == "SELECTED"
        assert d["failed_stage"] is None
        assert d["capacity_class"] == "subscription"
        assert d["rank"] == 1


# ---------------------------------------------------------------------------
# 3. _hydrate_sources
# ---------------------------------------------------------------------------


class TestHydrateSources:
    def test_empty_context(self) -> None:
        assert _hydrate_sources([], Path("/tmp"), 60000) == []

    def test_readable_file(self, tmp_path: Path) -> None:
        f = tmp_path / "src" / "main.py"
        f.parent.mkdir(parents=True)
        f.write_text("x = 1\n" * 100)
        sources = _hydrate_sources(["src/main.py"], tmp_path, 60000)
        assert len(sources) == 1
        s = sources[0]
        assert s["path"] == "src/main.py"
        assert s["bytes"] == f.stat().st_size
        assert s["included"] is True
        assert s["truncated_at"] is None
        assert s["reason"] is None

    def test_unreadable_file(self, tmp_path: Path) -> None:
        sources = _hydrate_sources(["no/such.py"], tmp_path, 60000)
        assert sources[0]["included"] is False
        assert sources[0]["reason"] == "unreadable"

    def test_truncated_file(self, tmp_path: Path) -> None:
        f = tmp_path / "big.py"
        f.write_bytes(b"x" * 5000)
        sources = _hydrate_sources(["big.py"], tmp_path, 3000)
        s = sources[0]
        assert s["included"] is True
        assert s["truncated_at"] == 3000
        assert s["reason"] == "truncated"

    def test_budget_exhausted(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f1.write_bytes(b"x" * 100)
        f2 = tmp_path / "b.py"
        f2.write_bytes(b"y" * 100)
        sources = _hydrate_sources(["a.py", "b.py"], tmp_path, 100)
        assert sources[0]["included"] is True
        assert sources[1]["included"] is False
        assert sources[1]["reason"] == "budget_exhausted"


# ---------------------------------------------------------------------------
# 4. Size bound: 7000 routes -> serialised eligibility event < 32 KB
# ---------------------------------------------------------------------------


def test_eligibility_event_size_bound_7000_routes() -> None:
    """Build an eligibility event from 7,000 fake routes and assert < 32 KB."""
    verdicts = [
        RouteVerdict(
            route_id=f"provider-{i % 30}/model-{i}",
            provider=f"provider-{i % 30}",
            reached=EligibilityStage.HEALTHY,
            failed_stage=EligibilityStage.AVAILABLE,
            reason="cooldown" if i % 3 == 0 else "probe_timeout",
        )
        for i in range(7000)
    ]
    counts = _ladder_counts(verdicts)
    rejections = _build_rejections(verdicts)
    candidates, omitted, summary = _build_candidates(verdicts, None)

    event_data: dict[str, Any] = {
        "type": "eligibility",
        "node_id": "alpha",
        **counts,
        "rejections": rejections,
        "candidates": candidates,
        "candidates_omitted": omitted,
        "omitted_summary": summary,
        "selected": None,
    }
    blob = json.dumps(event_data, default=str)
    assert len(blob.encode()) < 32_768, (
        f"eligibility event is {len(blob.encode())} bytes, expected < 32 KB"
    )
    assert len(candidates) <= _MAX_CANDIDATES
    assert omitted == 7000 - len(candidates)


# ---------------------------------------------------------------------------
# 5. Backward compatibility: existing proof runs load without error
# ---------------------------------------------------------------------------

_PROOF_DIRS = [
    "docs/proof/live-controller-run",
    "docs/proof/demo-run",
    "docs/proof/dogfood-bod-273-2026-09-28",
    "docs/proof/dogfood-bod-225-live-2026-09-29",
]


@pytest.mark.parametrize("rel", _PROOF_DIRS)
def test_existing_proof_run_receipt_still_valid(rel: str) -> None:
    """verify_run_receipt on committed proof runs returns no problems."""
    from verdict.orchestration.receipt import verify_run_receipt

    run_dir = Path(__file__).resolve().parent.parent / rel
    if not (run_dir / "events.jsonl").exists():
        pytest.skip(f"{run_dir} not found")
    problems = verify_run_receipt(run_dir)
    assert problems == [], f"receipt verification failed: {problems}"


@pytest.mark.parametrize("rel", _PROOF_DIRS)
def test_existing_proof_run_tui_replay(rel: str) -> None:
    """RunView can project every event in the committed proof runs."""
    from verdict.orchestration.tui import RunView

    run_dir = Path(__file__).resolve().parent.parent / rel
    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        pytest.skip(f"{events_path} not found")
    view = RunView()
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        view.apply(event)
    # If we get here without exception, backward compat holds.
    assert view.goal or view.topology or view.nodes
