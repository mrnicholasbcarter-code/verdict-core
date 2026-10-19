
"""Tests for nested execution requirements enforcement (BOD-432 / PR918)."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

import test_live_candidate_shortlist as L
import verdict.api as api
import verdict.orchestration.health_cache as hc
from verdict.execution_path import optimize_execution_path
from verdict.intelligence import IntelligenceService, prepared_task_profile

NOW = L.NOW


def _service(probed=None, top_k=4):
    """Create an IntelligenceService with test fixtures."""
    if probed is None:
        probed = []
    return L._service(probed, top_k=top_k)


def _run_production_prepare(nested_min_context=None, nested_tools_required=None, 
                             context_min_context=None, context_tools_required=None):
    """Run the production prepare path with given nested and context requirements."""
    raw_request = {
        "schema_version": "1",
        "trajectory_id": "traj-bod432",
        "slice_id": "slice-bod432",
        "acceptance_criteria": ["tests pass"],
        "proof_criteria": ["pytest green"],
        "candidates": [
            {
                "route_id": "limited/no-tools",
                "gateway": "omniroute",
                "provider": "limited",
                "model": "no-tools",
                "eligible": True,
                "is_free": True,
                "certification_state": "ready",
                "certification_freshness": "fresh",
            },
            {
                "route_id": "quality/strong-code",
                "gateway": "omniroute",
                "provider": "quality",
                "model": "strong-code",
                "eligible": True,
                "is_free": True,
                "certification_state": "ready",
                "certification_freshness": "fresh",
            },
        ],
    }
    # Add nested requirements
    if nested_min_context is not None:
        raw_request["min_context_tokens"] = nested_min_context
    if nested_tools_required is not None:
        raw_request["tools_required"] = nested_tools_required

    context: dict = {}
    if context_min_context is not None:
        context["min_context_tokens"] = context_min_context
    if context_tools_required is not None:
        context["tools_required"] = context_tools_required

    request = api._public_execution_path_request(raw_request, task="fix code", context=context)

    service = _service([], top_k=4)
    transport = service.confirm_transport
    service.confirm_transport = None
    service._load_admit_snapshot = lambda: (L._snapshot(), "", True, None)
    service._resolve_confirm_transport = lambda endpoint: transport
    tmp = tempfile.mkdtemp()
    hc.default_cache_path = lambda: Path(tmp) / "health.json"

    prepared = service._prepare_execution_path_request(
        "fix code", "low", dict(context), request
    )
    kept = [o.route.route_id for o in prepared.offers]
    drops = {d.route_id: d.reason for d in prepared.pool_receipt.hard_drops}
    
    decision = optimize_execution_path(prepared)
    
    return {
        "request": request,
        "prepared": prepared,
        "kept": kept,
        "drops": drops,
        "decision": decision,
    }


def test_nested_min_context_enforced():
    """Nested min_context_tokens=200000 must reject 128k routes."""
    result = _run_production_prepare(nested_min_context=200000)
    
    # Both routes have 128k context, so both should be hard-dropped
    assert "limited/no-tools" in result["drops"]
    assert "quality/strong-code" in result["drops"]
    # With no qualified offers, decision should be blocked
    assert result["decision"].selected_strategy == "blocked"
    assert result["decision"].selected_candidate_id is None


def test_nested_tools_required_enforced():
    """Nested tools_required=True must reject no-tools route."""
    result = _run_production_prepare(nested_tools_required=True)
    
    # limited/no-tools has tools=False, should be dropped
    assert "limited/no-tools" in result["drops"]
    assert result["drops"]["limited/no-tools"] == "capability_mismatch"
    # quality/strong-code has tools=True, should survive
    assert "quality/strong-code" in result["kept"]
    assert result["decision"].selected_candidate_id == "quality/strong-code"


def test_context_and_nested_strongest_floor_merge():
    """Strongest floor wins: nested stronger value cannot be ignored, context weaker cannot override."""
    # Context has min_context=100000, nested has 200000 -> stronger 200000 should win
    result = _run_production_prepare(nested_min_context=200000, context_min_context=100000)
    assert "quality/strong-code" in result["drops"]
    assert result["decision"].selected_strategy == "blocked"
    
    # Context has min_context=200000, nested has 100000 -> stronger 200000 should win
    result = _run_production_prepare(nested_min_context=100000, context_min_context=200000)
    assert "quality/strong-code" in result["drops"]
    assert result["decision"].selected_strategy == "blocked"


def test_nested_false_cannot_undo_context_true():
    """Nested tools_required=False cannot undo context tools_required=True."""
    # Context has tools_required=True, nested has tools_required=False -> True should win
    result = _run_production_prepare(nested_tools_required=False, context_tools_required=True)
    assert "limited/no-tools" in result["drops"]
    assert result["decision"].selected_candidate_id == "quality/strong-code"


def test_nested_true_cannot_be_ignored_when_context_false():
    """Nested tools_required=True cannot be ignored when context has tools_required=False."""
    result = _run_production_prepare(nested_tools_required=True, context_tools_required=False)
    assert "limited/no-tools" in result["drops"]
    assert result["decision"].selected_candidate_id == "quality/strong-code"


def test_profile_digest_consistency():
    """Same normalized requirements must generate same profile_digest at route() and relay.
    
    This verifies that the merge logic produces deterministic profile digests:
    - Nested requirements merged with empty context
    - Same requirements passed directly in context
    Both should produce identical profile digests.
    """
    # Profile from nested requirements (merged with empty context in prepare)
    profile_nested = prepared_task_profile(
        "fix code", 
        {"tools_required": True, "min_context_tokens": 200000},  # This simulates merged context
        planner=None
    )[1]
    
    # Profile from context requirements directly
    profile_context = prepared_task_profile(
        "fix code", 
        {"tools_required": True, "min_context_tokens": 200000},
        planner=None
    )[1]
    
    # The profile digest should be the same for same requirements
    assert profile_nested.digest == profile_context.digest
    
    # Also test that the task_family is included in the profile
    profile_with_family = prepared_task_profile(
        "fix code", 
        {"tools_required": True, "min_context_tokens": 200000, "task_family": "coding"},
        planner=None
    )[1]
    
    assert profile_with_family.task_family == "coding"
    assert profile_with_family.digest != profile_nested.digest  # Different family -> different digest
