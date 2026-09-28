"""Tests for BOD-203 AC5/AC6/AC7: calibration roles, per-category costs, per-role reports."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from verdict.decision_signals.calibration import (
    ROLES,
    CalibrationRecord,
    _evaluate_slice,
    evaluate,
    load_records,
    render_markdown,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rec(
    task_id: str = "t1",
    task_class: str = "bounded_implementation",
    frontier_worthy: float | None = 0.8,
    confidence: float | None = 0.9,
    security_sensitive: float | None = 0.1,
    frontier_needed: bool = False,
    security_relevant: bool = False,
    planner_was_frontier: bool = False,
    verified: bool = True,
    first_pass: bool = True,
    retries: int = 0,
    escalations: int = 0,
    total_cost_usd: float = 1.0,
    role: str | None = None,
    context_cost_usd: float = 0.0,
    tool_cost_usd: float = 0.0,
    retry_cost_usd: float = 0.0,
    verification_cost_usd: float = 0.0,
    escalation_cost_usd: float = 0.0,
) -> CalibrationRecord:
    return CalibrationRecord(
        task_id=task_id,
        task_class=task_class,
        frontier_worthy=frontier_worthy,
        confidence=confidence,
        security_sensitive=security_sensitive,
        frontier_needed=frontier_needed,
        security_relevant=security_relevant,
        planner_was_frontier=planner_was_frontier,
        verified=verified,
        first_pass=first_pass,
        retries=retries,
        escalations=escalations,
        total_cost_usd=total_cost_usd,
        role=role,
        context_cost_usd=context_cost_usd,
        tool_cost_usd=tool_cost_usd,
        retry_cost_usd=retry_cost_usd,
        verification_cost_usd=verification_cost_usd,
        escalation_cost_usd=escalation_cost_usd,
    )


# ---------------------------------------------------------------------------
# AC5: role field on CalibrationRecord
# ---------------------------------------------------------------------------


class TestAC5RoleField:
    """CalibrationRecord.role: closed-set validation, optional/backward-compat."""

    def test_valid_roles_accepted(self) -> None:
        for role in ROLES:
            r = _rec(role=role)
            assert r.role == role

    def test_none_role_accepted(self) -> None:
        """Missing role -> None; backward compatible with legacy JSONL."""
        r = _rec(role=None)
        assert r.role is None

    def test_unknown_role_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown role"):
            _rec(role="janitor")

    def test_empty_string_role_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown role"):
            _rec(role="")

    def test_from_dict_with_role(self) -> None:
        raw = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
            "role": "controller",
        }
        r = CalibrationRecord.from_dict(raw)
        assert r.role == "controller"

    def test_from_dict_without_role(self) -> None:
        """Legacy JSONL without role field -> None."""
        raw = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
        }
        r = CalibrationRecord.from_dict(raw)
        assert r.role is None

    def test_from_dict_unknown_role_rejected(self) -> None:
        raw = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
            "role": "invalid_role",
        }
        with pytest.raises(ValueError, match="unknown role"):
            CalibrationRecord.from_dict(raw)

    def test_load_records_with_role(self) -> None:
        """load_records roundtrips role field from JSONL."""
        record = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
            "role": "implementation_worker",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(record) + "\n")
            path = Path(f.name)
        try:
            loaded = load_records(path)
            assert len(loaded) == 1
            assert loaded[0].role == "implementation_worker"
        finally:
            path.unlink()

    def test_load_records_rejects_bad_role(self) -> None:
        """load_records propagates role validation error."""
        record = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
            "role": "hacker",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(record) + "\n")
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="invalid calibration record"):
                load_records(path)
        finally:
            path.unlink()

    def test_roles_tuple_is_closed_set(self) -> None:
        assert ROLES == (
            "controller",
            "implementation_worker",
            "research_test_worker",
            "independent_reviewer",
        )

    def test_existing_fixture_backward_compat(self) -> None:
        """Existing replay_sample.jsonl loads without error (no role field)."""
        fixtures = Path(__file__).parent / "fixtures" / "openjev" / "calibration"
        records = load_records(fixtures / "replay_sample.jsonl")
        assert len(records) >= 60
        assert all(r.role is None for r in records)


# ---------------------------------------------------------------------------
# AC6: per-category cost fields
# ---------------------------------------------------------------------------


class TestAC6PerCategoryCost:
    """Per-category cost fields on CalibrationRecord and CalibrationReport."""

    def test_record_defaults_zero(self) -> None:
        r = _rec()
        assert r.context_cost_usd == 0.0
        assert r.tool_cost_usd == 0.0
        assert r.retry_cost_usd == 0.0
        assert r.verification_cost_usd == 0.0
        assert r.escalation_cost_usd == 0.0

    def test_record_accepts_cost_values(self) -> None:
        r = _rec(
            context_cost_usd=0.10,
            tool_cost_usd=0.20,
            retry_cost_usd=0.05,
            verification_cost_usd=0.03,
            escalation_cost_usd=0.02,
        )
        assert r.context_cost_usd == 0.10
        assert r.tool_cost_usd == 0.20

    def test_from_dict_with_costs(self) -> None:
        raw = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
            "context_cost_usd": 0.15,
            "tool_cost_usd": 0.25,
        }
        r = CalibrationRecord.from_dict(raw)
        assert r.context_cost_usd == 0.15
        assert r.tool_cost_usd == 0.25
        assert r.retry_cost_usd == 0.0  # default

    def test_from_dict_without_costs(self) -> None:
        """Legacy JSONL without cost fields -> zero defaults."""
        raw = {
            "task_id": "t1",
            "task_class": "bounded_implementation",
            "frontier_worthy": 0.5,
            "confidence": 0.9,
            "security_sensitive": 0.1,
            "frontier_needed": False,
            "security_relevant": False,
            "planner_was_frontier": False,
            "verified": True,
            "first_pass": True,
        }
        r = CalibrationRecord.from_dict(raw)
        assert r.context_cost_usd == 0.0

    def test_evaluate_aggregates_per_category_costs(self) -> None:
        records = [
            _rec(
                task_id="a",
                context_cost_usd=0.10,
                tool_cost_usd=0.20,
                retry_cost_usd=0.05,
                verification_cost_usd=0.03,
                escalation_cost_usd=0.02,
                total_cost_usd=0.40,
            ),
            _rec(
                task_id="b",
                context_cost_usd=0.20,
                tool_cost_usd=0.30,
                retry_cost_usd=0.10,
                verification_cost_usd=0.07,
                escalation_cost_usd=0.03,
                total_cost_usd=0.70,
            ),
        ]
        report = evaluate(records)
        assert abs(report.context_cost_usd - 0.30) < 1e-6
        assert abs(report.tool_cost_usd - 0.50) < 1e-6
        assert abs(report.retry_cost_usd - 0.15) < 1e-6
        assert abs(report.verification_cost_usd - 0.10) < 1e-6
        assert abs(report.escalation_cost_usd - 0.05) < 1e-6
        assert abs(report.total_cost_usd - 1.10) < 1e-6

    def test_cost_per_verified_completion(self) -> None:
        """total cost / verified completions."""
        records = [
            _rec(task_id="a", total_cost_usd=2.0, verified=True),
            _rec(task_id="b", total_cost_usd=3.0, verified=True),
            _rec(task_id="c", total_cost_usd=5.0, verified=False),
        ]
        report = evaluate(records)
        # total=10.0, verified=2 -> 5.0
        assert report.cost_per_verified_completion is not None
        assert abs(report.cost_per_verified_completion - 5.0) < 1e-6

    def test_cost_per_verified_completion_no_verified(self) -> None:
        records = [_rec(task_id="a", total_cost_usd=2.0, verified=False)]
        report = evaluate(records)
        assert report.cost_per_verified_completion is None

    def test_report_to_dict_includes_costs(self) -> None:
        records = [
            _rec(task_id="a", context_cost_usd=0.10, tool_cost_usd=0.20, total_cost_usd=0.30)
        ]
        report = evaluate(records)
        d = report.to_dict()
        assert "context_cost_usd" in d
        assert "tool_cost_usd" in d
        assert "cost_per_verified_completion" in d


# ---------------------------------------------------------------------------
# AC7: per-role and per-task-class reports and recommendations
# ---------------------------------------------------------------------------


class TestAC7PerRoleReports:
    """evaluate/recommend produce per-role breakdowns; thin slices stay SHADOW."""

    def test_no_roles_means_empty_per_role(self) -> None:
        """Legacy records without role -> per_role is empty."""
        records = [_rec(task_id=f"t{i}") for i in range(5)]
        report = evaluate(records)
        assert report.per_role == {}
        assert report.per_role_recommendations == {}

    def test_per_role_populated_when_roles_present(self) -> None:
        records = [
            _rec(task_id="c1", role="controller", total_cost_usd=1.0),
            _rec(task_id="w1", role="implementation_worker", total_cost_usd=2.0),
            _rec(task_id="w2", role="implementation_worker", total_cost_usd=3.0),
        ]
        report = evaluate(records)
        assert "controller" in report.per_role
        assert "implementation_worker" in report.per_role
        assert report.per_role["controller"]["records"] == 1
        assert report.per_role["implementation_worker"]["records"] == 2

    def test_per_role_cost_breakdown(self) -> None:
        records = [
            _rec(
                task_id="w1",
                role="implementation_worker",
                total_cost_usd=5.0,
                context_cost_usd=2.0,
                tool_cost_usd=3.0,
            ),
            _rec(
                task_id="w2",
                role="implementation_worker",
                total_cost_usd=3.0,
                context_cost_usd=1.0,
                tool_cost_usd=2.0,
            ),
        ]
        report = evaluate(records)
        rm = report.per_role["implementation_worker"]
        assert abs(rm["total_cost_usd"] - 8.0) < 1e-6
        assert abs(rm["context_cost_usd"] - 3.0) < 1e-6
        assert abs(rm["tool_cost_usd"] - 5.0) < 1e-6

    def test_per_role_cost_per_verified(self) -> None:
        records = [
            _rec(task_id="w1", role="controller", total_cost_usd=4.0, verified=True),
            _rec(task_id="w2", role="controller", total_cost_usd=6.0, verified=True),
        ]
        report = evaluate(records)
        rm = report.per_role["controller"]
        # 10.0 / 2 = 5.0
        assert rm["cost_per_verified_completion"] is not None
        assert abs(rm["cost_per_verified_completion"] - 5.0) < 1e-6

    def test_insufficient_per_role_samples_yield_shadow(self) -> None:
        """Roles with < MIN_RECORDS_FOR_ADVISORY usable signals -> SHADOW."""
        # 3 records with role, well below 50 threshold
        records = [_rec(task_id=f"w{i}", role="controller") for i in range(3)]
        report = evaluate(records)
        state, reasons = report.per_role_recommendations["controller"]
        assert state == "SHADOW"
        assert any("usable signals" in r for r in reasons)

    def test_mixed_roles_and_none(self) -> None:
        """Records with role=None are excluded from per_role but included globally."""
        records = [
            _rec(task_id="legacy", role=None, total_cost_usd=1.0),
            _rec(task_id="w1", role="controller", total_cost_usd=2.0),
        ]
        report = evaluate(records)
        assert report.records == 2
        assert "controller" in report.per_role
        assert report.per_role["controller"]["records"] == 1
        # None-role records not in per_role at all
        assert len(report.per_role) == 1

    def test_per_role_fn_rates_independent(self) -> None:
        """Each role slice computes its own FN rate independently."""
        records = [
            # controller: 1 FN (frontier_needed=True, predicted low)
            _rec(
                task_id="c1",
                role="controller",
                frontier_needed=True,
                frontier_worthy=0.2,
                verified=True,
            ),
            _rec(
                task_id="c2",
                role="controller",
                frontier_needed=False,
                frontier_worthy=0.8,
                verified=True,
            ),
            # worker: 0 FN
            _rec(
                task_id="w1",
                role="implementation_worker",
                frontier_needed=True,
                frontier_worthy=0.8,
                verified=True,
            ),
            _rec(
                task_id="w2",
                role="implementation_worker",
                frontier_needed=False,
                frontier_worthy=0.2,
                verified=True,
            ),
        ]
        report = evaluate(records, threshold=0.5)
        c_fn = report.per_role["controller"]["frontier_fn_rate"]
        w_fn = report.per_role["implementation_worker"]["frontier_fn_rate"]
        assert c_fn == 1.0  # 1/1 needed
        assert w_fn == 0.0  # 0/1 needed


class TestAC7PerRoleRecommendations:
    """Per-role recommendation uses same thresholds; insufficient data -> SHADOW."""

    def test_all_roles_shadow_when_few_records(self) -> None:
        records = []
        for i, role in enumerate(ROLES):
            records.append(_rec(task_id=f"r{i}", role=role))
        report = evaluate(records)
        for role in ROLES:
            state, _ = report.per_role_recommendations[role]
            assert state == "SHADOW", f"{role} should be SHADOW with 1 record"

    def test_per_role_never_promotes_beyond_evidence(self) -> None:
        """Even if global report is ADVISORY, thin per-role slices stay SHADOW."""
        # Build enough global records to pass ADVISORY (50+ usable)
        records = []
        for i in range(60):
            records.append(
                _rec(
                    task_id=f"t{i}",
                    role="implementation_worker" if i < 5 else None,
                    frontier_needed=False,
                    frontier_worthy=0.8,
                    security_relevant=True,
                    security_sensitive=0.9,
                )
            )
        report = evaluate(records)
        # Global might be something other than SHADOW
        # But per-role with only 5 records must be SHADOW
        state, reasons = report.per_role_recommendations["implementation_worker"]
        assert state == "SHADOW"
        assert any("usable signals" in r for r in reasons)


# ---------------------------------------------------------------------------
# AC7: render_markdown includes per-role breakdown
# ---------------------------------------------------------------------------


class TestAC7RenderMarkdown:
    """render_markdown shows per-role sections when roles are present."""

    def test_no_per_role_section_without_roles(self) -> None:
        records = [_rec(task_id=f"t{i}") for i in range(3)]
        report = evaluate(records)
        md = render_markdown(report)
        assert "## Per-role breakdown" not in md

    def test_per_role_section_present_with_roles(self) -> None:
        records = [
            _rec(
                task_id="c1",
                role="controller",
                total_cost_usd=1.0,
                context_cost_usd=0.5,
                tool_cost_usd=0.5,
            ),
            _rec(
                task_id="w1",
                role="implementation_worker",
                total_cost_usd=2.0,
                context_cost_usd=1.0,
                tool_cost_usd=1.0,
            ),
        ]
        report = evaluate(records)
        md = render_markdown(report)
        assert "## Per-role breakdown" in md
        assert "### controller" in md
        assert "### implementation_worker" in md

    def test_per_role_section_shows_costs(self) -> None:
        records = [
            _rec(
                task_id="w1",
                role="implementation_worker",
                total_cost_usd=3.0,
                context_cost_usd=1.0,
                tool_cost_usd=2.0,
            )
        ]
        report = evaluate(records)
        md = render_markdown(report)
        assert "context=$" in md
        assert "tool=$" in md

    def test_per_role_section_shows_recommendation(self) -> None:
        records = [_rec(task_id="c1", role="controller")]
        report = evaluate(records)
        md = render_markdown(report)
        assert "**SHADOW**" in md

    def test_global_cost_breakdown_in_markdown(self) -> None:
        """Top-level report shows per-category cost breakdown."""
        records = [
            _rec(
                task_id="a",
                context_cost_usd=0.10,
                tool_cost_usd=0.20,
                retry_cost_usd=0.05,
                verification_cost_usd=0.03,
                escalation_cost_usd=0.02,
                total_cost_usd=0.40,
            )
        ]
        report = evaluate(records)
        md = render_markdown(report)
        assert "cost breakdown:" in md
        assert "context=$" in md
        assert "cost per verified completion:" in md

    def test_cost_per_verified_in_markdown(self) -> None:
        records = [
            _rec(task_id="a", total_cost_usd=4.0, verified=True),
            _rec(task_id="b", total_cost_usd=6.0, verified=False),
        ]
        report = evaluate(records)
        md = render_markdown(report)
        # 10.0 / 1 verified = $10.0
        assert "$10.0000" in md


# ---------------------------------------------------------------------------
# Backward compatibility: existing fixtures still work
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """Existing JSONL without new fields loads and evaluates identically."""

    def test_evaluate_existing_fixture(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "openjev" / "calibration"
        records = load_records(fixtures / "replay_sample.jsonl")
        report = evaluate(records)
        # All new fields present with defaults
        assert report.context_cost_usd == 0.0
        assert report.tool_cost_usd == 0.0
        assert report.per_role == {}
        # Existing fields still work
        assert report.records >= 60
        assert report.recommended_state in ("SHADOW", "ADVISORY", "BOUNDED_SKIP")

    def test_to_dict_schema_unchanged(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "openjev" / "calibration"
        records = load_records(fixtures / "replay_sample.jsonl")
        report = evaluate(records)
        d = report.to_dict()
        assert d["schema"] == "verdict.openjev-calibration/v1"


# ---------------------------------------------------------------------------
# _evaluate_slice: unit tests for the shared helper
# ---------------------------------------------------------------------------


class TestEvaluateSlice:
    """Direct tests for _evaluate_slice used by both global and per-role paths."""

    def test_returns_all_cost_keys(self) -> None:
        records = [_rec(task_id="a", context_cost_usd=0.5, total_cost_usd=1.0)]
        m = _evaluate_slice(records, threshold=0.5, min_confidence=0.6, security_threshold=0.5)
        for key in (
            "context_cost_usd",
            "tool_cost_usd",
            "retry_cost_usd",
            "verification_cost_usd",
            "escalation_cost_usd",
            "cost_per_verified_completion",
        ):
            assert key in m

    def test_cost_per_verified_none_when_no_verified(self) -> None:
        records = [_rec(task_id="a", verified=False, total_cost_usd=1.0)]
        m = _evaluate_slice(records, threshold=0.5, min_confidence=0.6, security_threshold=0.5)
        assert m["cost_per_verified_completion"] is None
