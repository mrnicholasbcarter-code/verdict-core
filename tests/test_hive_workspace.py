"""Shared Hive Workspace pipeline tests (CONTEXT-001, #257)."""

from __future__ import annotations

import pytest

from verdict.hive_workspace import (
    PROPOSAL_PENDING,
    PROPOSAL_REJECTED,
    HiveWorkspace,
    Proposal,
    WorkerResult,
)
from verdict.memory_plane import MemoryPlane

TASK_ID = "task-1"


def _result(content: str = "worker output") -> WorkerResult:
    return WorkerResult(worker_id="w1", content=content, result_type="artifact")


def _workspace(tmp_path) -> HiveWorkspace:
    return HiveWorkspace(MemoryPlane(tmp_path / "memory.db"))


class TestSubmission:
    def test_submit_quarantines_as_pending(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        proposal = ws.submit_proposal(TASK_ID, _result("raw result"))
        assert proposal.status == PROPOSAL_PENDING
        assert not proposal.verified
        assert ws.pending() == [proposal]
        assert ws.verified() == []
        # Quarantined content is not committed to shared knowledge.
        assert ws.get(proposal.proposal_id).worker_result.content == "raw result"

    def test_duplicate_proposal_id_rejected(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        ws.submit_proposal(TASK_ID, _result("one"), proposal_id="proposal:fixed")
        with pytest.raises(ValueError):
            ws.submit_proposal(TASK_ID, _result("two"), proposal_id="proposal:fixed")


class TestVerification:
    def test_verified_proposal_commits_to_shared_knowledge(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        proposal = ws.submit_proposal(TASK_ID, _result("verified content"))
        outcome = ws.verify(proposal, lambda p: True)
        assert outcome.passed
        assert outcome.resulting_proposal.verified
        assert ws.pending() == []
        assert ws.verified() == [outcome.resulting_proposal]
        # The committed record carries verified trust, not quarantine.
        record = ws.memory.get("hive-workspace", "proposal:task-1")
        assert record is not None
        assert record.trust == "hive-verified"
        assert record.authority_verified

    def test_unverified_stays_quarantined_when_gate_returns_false(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        proposal = ws.submit_proposal(TASK_ID, _result())
        outcome = ws.verify(proposal, lambda p: False)
        assert not outcome.passed
        assert outcome.resulting_proposal.status == PROPOSAL_REJECTED
        assert outcome.resulting_proposal.reason == "verification_gate_rejected"
        assert ws.pending() == []
        assert ws.verified() == []
        assert ws.get(proposal.proposal_id).status == PROPOSAL_REJECTED

    def test_unverified_stays_quarantined_when_gate_raises(self, tmp_path) -> None:
        def gate(_proposal: Proposal) -> bool:
            raise ValueError("policy: protected work requires fresh availability")

        ws = _workspace(tmp_path)
        proposal = ws.submit_proposal(TASK_ID, _result())
        outcome = ws.verify(proposal, gate)
        assert not outcome.passed
        assert outcome.reason == "policy: protected work requires fresh availability"
        assert ws.get(proposal.proposal_id).status == PROPOSAL_REJECTED

    def test_verify_resolved_proposal_raises(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        proposal = ws.submit_proposal(TASK_ID, _result())
        ws.verify(proposal, lambda p: True)
        with pytest.raises(ValueError):
            ws.verify(ws.get(proposal.proposal_id), lambda p: True)


class TestPersistence:
    def test_proposals_survive_reopen(self, tmp_path) -> None:
        path = tmp_path / "memory.db"
        ws = HiveWorkspace(MemoryPlane(path))
        proposal = ws.submit_proposal(TASK_ID, _result("durable"))
        ws.verify(proposal, lambda p: True)

        reopened = HiveWorkspace(MemoryPlane(path))
        assert [p.proposal_id for p in reopened.verified()] == [proposal.proposal_id]

    def test_rejected_leave_no_shared_content(self, tmp_path) -> None:
        ws = _workspace(tmp_path)
        proposal = ws.submit_proposal(TASK_ID, _result("secret candidate"))
        ws.verify(proposal, lambda p: False)
        for record in ws.memory.records(namespace="hive-workspace", include_history=True):
            assert "secret candidate" not in record.content
        assert ws.get(proposal.proposal_id).status == PROPOSAL_REJECTED
