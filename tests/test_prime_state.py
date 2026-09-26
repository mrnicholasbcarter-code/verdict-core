import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    path = ROOT / "scripts/prime_state.py"
    assert path.exists(), "state contracts missing"
    spec = importlib.util.spec_from_file_location("prime_state", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def packet(worktree):
    return {
        "schema_version": 1,
        "issue": "BOD-12",
        "issue_url": "https://example.com/issue-tracker/BOD-12",
        "project": "Verdict",
        "team": "BOD",
        "issue_updated_at": "2026-09-13T00:00:00Z",
        "objective": "Verify workflow discovery",
        "acceptance_criteria": [{"id": "AC1", "text": "Discovered", "verification": "loader test"}],
        "dependencies": [],
        "context": {"refs": ["AGENTS.md"], "omissions": []},
        "spec": "spec.md",
        "plan": "plan.md",
        "tasks": "tasks.md",
        "worktree": str(worktree),
        "branch": "issue",
        "base_sha": "a" * 40,
        "ownership": "test-owner",
        "constraints": ["No unrelated edits"],
        "allowed_files": ["README"],
        "provider": "omniroute-live",
        "model": "gc/grok-4.6",
        "routing_evidence": "route.json",
        "available_at": "2026-09-13T00:00:00Z",
        "attempt_id": "attempt-1",
        "budgets": {"wall_seconds": 3600, "idle_seconds": 600, "ci_seconds": 1200},
        "return_evidence": "receipt.json",
        "proof_requirements": {
            g: {"required": True, "command": "pytest"}
            for g in ("STATIC", "UNIT", "INTEGRATION", "ACCEPTANCE-PROOF")
        },
    }


def test_lease_acquire_blocks_second_writer_and_allows_stale_takeover(tmp_path):
    m = module()
    state = tmp_path
    lease = m.acquire_lease(
        state,
        issue="BOD-12",
        dispatch_id="d1",
        worker_id="w1",
        worktree="/wt",
        branch="b",
        stale_after_seconds=600,
        supervisor_id="sup",
        now=0.0,
    )
    assert lease["generation"] == 1 and lease["status"] == "ACTIVE"
    with pytest.raises(ValueError, match="held"):
        m.acquire_lease(
            state,
            issue="BOD-12",
            dispatch_id="d2",
            worker_id="w2",
            worktree="/wt",
            branch="b",
            stale_after_seconds=600,
            supervisor_id="sup",
            now=10.0,
        )
    # stale by progress deadline -> fenced takeover with higher generation
    taken = m.acquire_lease(
        state,
        issue="BOD-12",
        dispatch_id="d2",
        worker_id="w2",
        worktree="/wt",
        branch="b",
        stale_after_seconds=600,
        supervisor_id="sup",
        now=10_000.0,
    )
    assert taken["generation"] == 2 and taken["status"] == "ACTIVE"
    old = m.read_lease_history(state, "BOD-12")
    assert [entry["generation"] for entry in old] == [1, 2]
    assert old[0]["status"] == "SUPERSEDED"


def test_fenced_writer_cannot_heartbeat_or_finish(tmp_path):
    m = module()
    m.acquire_lease(
        tmp_path,
        issue="BOD-12",
        dispatch_id="d1",
        worker_id="w1",
        worktree="/wt",
        branch="b",
        stale_after_seconds=600,
        supervisor_id="sup",
        now=0.0,
    )
    m.acquire_lease(
        tmp_path,
        issue="BOD-12",
        dispatch_id="d2",
        worker_id="w2",
        worktree="/wt",
        branch="b",
        stale_after_seconds=600,
        supervisor_id="sup",
        now=10_000.0,
    )
    with pytest.raises(ValueError, match="fenced"):
        m.heartbeat_lease(tmp_path, "BOD-12", generation=1, progress=True, now=10_001.0)
    fenced = {
        "schema_version": 1,
        "issue": "BOD-12",
        "dispatch_id": "d1",
        "generation": 1,
        "worktree": "/wt",
        "status": "SUPERSEDED",
    }
    with pytest.raises(ValueError):
        m.require_lease_for_pr(tmp_path, packet("/wt"), lease_hint=fenced)


def test_active_lease_required_before_pr(tmp_path):
    m = module()
    p = packet("/wt")
    with pytest.raises(ValueError, match="lease"):
        m.require_lease_for_pr(tmp_path, p)
    m.acquire_lease(
        tmp_path,
        issue=p["issue"],
        dispatch_id=p["attempt_id"],
        worker_id="w1",
        worktree="/wt",
        branch=p["branch"],
        stale_after_seconds=600,
        supervisor_id="sup",
        now=0.0,
    )
    m.require_lease_for_pr(tmp_path, p, now=1.0)
    with pytest.raises(ValueError, match="stale"):
        m.require_lease_for_pr(tmp_path, p, now=10_000.0)


def test_supersede_stale_leases_recovers_without_manual_kill(tmp_path):
    m = module()
    m.acquire_lease(
        tmp_path,
        issue="BOD-12",
        dispatch_id="d1",
        worker_id="w1",
        worktree="/wt",
        branch="b",
        stale_after_seconds=600,
        supervisor_id="sup",
        now=0.0,
    )
    m.acquire_lease(
        tmp_path,
        issue="BOD-13",
        dispatch_id="d9",
        worker_id="w9",
        worktree="/wt2",
        branch="b2",
        stale_after_seconds=600,
        supervisor_id="sup",
        now=9_900.0,
    )
    reaped = m.supersede_stale_leases(tmp_path, now=10_000.0)
    assert reaped == ["BOD-12"]
    assert m.read_lease(tmp_path, "BOD-12")["status"] == "SUPERSEDED"
    assert m.read_lease(tmp_path, "BOD-13")["status"] == "ACTIVE"


def test_checkpoint_roundtrip_requires_full_schema(tmp_path):
    m = module()
    cp = m.checkpoint_skeleton(issue="BOD-12", state="IMPLEMENTING")
    with pytest.raises(ValueError):
        m.validate_checkpoint({k: v for k, v in cp.items() if k != "next_action"})
    with pytest.raises(ValueError):
        m.validate_checkpoint({**cp, "state": "NOT_A_STATE"})
    written = m.write_checkpoint(tmp_path, cp)
    assert written.exists()
    loaded = m.read_checkpoint(tmp_path)
    assert loaded["issue"] == "BOD-12" and loaded["updated_at"]
    # cold resume: a fresh process can recover issue/state/next action from disk only
    assert m.next_action_from_checkpoint(tmp_path)["state"] == "IMPLEMENTING"


def test_checkpoint_blocks_regression_from_merged_without_main_proof(tmp_path):
    m = module()
    cp = m.checkpoint_skeleton(issue="BOD-12", state="MERGED")
    m.write_checkpoint(tmp_path, cp)
    with pytest.raises(ValueError):
        m.write_checkpoint(tmp_path, m.checkpoint_skeleton(issue="BOD-12", state="PROOF_COMPLETE"))


def test_ci_classification_is_deterministic():
    m = module()
    ok = {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS", "required": True}
    assert m.classify_ci([ok]) == "GREEN"
    assert m.classify_ci([]) == "EMPTY"
    assert (
        m.classify_ci(
            [{"name": "test", "status": "IN_PROGRESS", "conclusion": None, "required": True}]
        )
        == "PENDING"
    )
    assert (
        m.classify_ci(
            [ok, {"name": "lint", "status": "COMPLETED", "conclusion": "FAILURE", "required": True}]
        )
        == "CODE_FAILURE"
    )
    assert (
        m.classify_ci(
            [
                {
                    "name": "runner-availability",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                    "required": True,
                }
            ]
        )
        == "INFRA_FAILURE"
    )
    assert (
        m.classify_ci(
            [{"name": "test", "status": "COMPLETED", "conclusion": "CANCELLED", "required": True}]
        )
        == "CANCELLED"
    )
    assert (
        m.classify_ci(
            [
                {
                    "name": "optional",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "required": False,
                }
            ]
        )
        == "MISSING_REQUIRED"
    )
    assert (
        m.classify_ci(
            [{"name": "test", "status": "COMPLETED", "conclusion": None, "required": True}]
        )
        == "PENDING"
    )
    assert m.classify_merge_state("MERGEABLE", "CLEAN") == "MERGEABLE"
    assert m.classify_merge_state("CONFLICTING", "DIRTY") == "CONFLICT"
    assert m.classify_merge_state("UNKNOWN", "UNKNOWN") == "UNKNOWN"


def test_ci_failure_routes_back_to_implementation_not_success():
    m = module()
    plan = m.ci_recovery_action(classification="CODE_FAILURE", attempts=0, max_attempts=2)
    assert plan["action"] == "CORRECTIVE_IMPLEMENTATION" and plan["state"] == "IMPLEMENTING"
    assert m.ci_recovery_action("INFRA_FAILURE", 0, 2)["action"] == "RETRY_CI"
    assert m.ci_recovery_action("CONFLICT", 0, 2)["action"] == "REBASE_AND_REPROOF"
    assert m.ci_recovery_action("CODE_FAILURE", 2, 2)["action"] == "BLOCKED"
    assert m.ci_recovery_action("GREEN", 0, 2)["action"] == "PROCEED_MERGE"


def test_progress_ignores_unregistered_artifact_churn():
    m = module()
    sub = "source-digest"
    art = "artifact-digest"
    rid = "receipt-identity"
    assert m.is_progress(sub, art, rid, sub, art, rid) is False
    assert m.is_progress(sub, art, rid, "new-source", art, rid) is True
    assert m.is_progress(sub, art, rid, sub, "new-artifact", rid) is False
    assert m.is_progress(sub, art, rid, sub, "new-artifact", "new-receipt") is True


def test_lease_hint_must_match_current_generation_record(tmp_path):
    m = module()
    packet_value = packet("/wt")
    current = m.acquire_lease(
        tmp_path,
        issue=packet_value["issue"],
        dispatch_id=packet_value["attempt_id"],
        worker_id="w1",
        worktree="/wt",
        branch=packet_value["branch"],
        stale_after_seconds=600,
        supervisor_id="sup",
        now=0.0,
    )
    with pytest.raises(ValueError, match="generation"):
        m.require_lease_for_pr(
            tmp_path, packet_value, now=1.0, lease_hint={**current, "worker_id": "spoofed"}
        )
    assert (tmp_path / "leases/BOD-12/lease.lock").exists()
