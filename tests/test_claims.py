"""Tests for verdict.orchestration.claims (BOD-279)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_flagship_failover_scenario import (
    CONNECTIONS,
    GRAPH,
    INVENTORY,
    ROUTE_A,
    ROUTE_B,
    OcrCli,
    _init_repo,
    _load_events,
    _run_scenario,
    _worker_script,
)
from tests.test_orch_eligibility import FakeProbe
from verdict.orchestration.claims import (
    CLAIM_STATUS_CONTRADICTED,
    CLAIM_STATUS_NOT_OBSERVED,
    CLAIM_STATUS_VERIFIED,
    Claim,
    derive_claims,
)
from verdict.orchestration.contracts import (
    RunOutcome,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.executors import ScriptedExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.review import OpenCodeReviewer
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _claims_dict(claims: list[Claim]) -> dict[str, Claim]:
    return {c.id: c for c in claims}


# ---------------------------------------------------------------------------
# Flagship failover scenario (with failure -> cooldown -> replacement)
# ---------------------------------------------------------------------------


class TestClaimsFlagshipFailover:
    """Claims derived from the flagship failover scenario."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
        self.events = result.events
        self.receipt = result.receipt
        self.run_dir = result.run_dir
        self.claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        self.by_id = _claims_dict(self.claims)

    def test_all_claims_present(self) -> None:
        expected_ids = {
            "task_aware_selection",
            "capability_filtering",
            "health_considered",
            "explicit_assignment",
            "failure_isolated",
            "automatic_failover",
            "cooldown_recorded",
            "context_within_budget",
            "replacement_completed",
            "validation_passed",
            "independent_review",
            "receipt_integrity",
        }
        assert set(self.by_id.keys()) == expected_ids

    def test_task_aware_selection_verified(self) -> None:
        c = self.by_id["task_aware_selection"]
        assert c.status == CLAIM_STATUS_VERIFIED
        assert len(c.evidence) > 0

    def test_health_considered_verified(self) -> None:
        c = self.by_id["health_considered"]
        assert c.status == CLAIM_STATUS_VERIFIED

    def test_explicit_assignment_verified(self) -> None:
        c = self.by_id["explicit_assignment"]
        assert c.status == CLAIM_STATUS_VERIFIED
        # All dispatches should be concrete routes (not auto/*)
        for ev in c.evidence:
            assert not ev.value["route_id"].startswith("auto/")

    def test_failure_isolated_verified(self) -> None:
        c = self.by_id["failure_isolated"]
        assert c.status == CLAIM_STATUS_VERIFIED
        # Evidence includes at least one failure event and run_finished
        sources = {e.source.split(":")[0] for e in c.evidence}
        assert "event" in sources

    def test_automatic_failover_verified(self) -> None:
        c = self.by_id["automatic_failover"]
        assert c.status == CLAIM_STATUS_VERIFIED
        # At least one reassignment from ROUTE_A to ROUTE_B
        reassign_evidence = [
            e for e in c.evidence
            if e.value.get("from_route") == ROUTE_A and e.value.get("to_route") == ROUTE_B
        ]
        assert len(reassign_evidence) >= 1

    def test_cooldown_recorded_verified(self) -> None:
        c = self.by_id["cooldown_recorded"]
        assert c.status == CLAIM_STATUS_VERIFIED

    def test_replacement_completed_verified(self) -> None:
        c = self.by_id["replacement_completed"]
        assert c.status == CLAIM_STATUS_VERIFIED
        # node-1 should be validated on a route different from failed route_a
        for ev in c.evidence:
            assert ev.value["success_route"] != ROUTE_A
            assert ev.value["final_state"] == "VALIDATED"

    def test_validation_passed_verified(self) -> None:
        c = self.by_id["validation_passed"]
        assert c.status == CLAIM_STATUS_VERIFIED

    def test_receipt_integrity_verified(self) -> None:
        c = self.by_id["receipt_integrity"]
        assert c.status == CLAIM_STATUS_VERIFIED

    def test_claims_serialise_round_trip(self) -> None:
        for claim in self.claims:
            d = claim.to_dict()
            assert d["id"] == claim.id
            assert d["status"] == claim.status
            assert isinstance(d["evidence"], list)


# ---------------------------------------------------------------------------
# Clean run (no failure) — failover/replacement claims NOT_OBSERVED
# ---------------------------------------------------------------------------


def _clean_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    """Run the graph with NO faults — all nodes succeed first time."""
    repo = _init_repo(tmp_path)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    state_path = tmp_path / "ladder-state.json"

    ladder = EligibilityLadder(INVENTORY, CONNECTIONS, FakeProbe(), state_path)
    executor = ScriptedExecutor(_worker_script)  # No faults
    classifier = FailureIntelligence()

    ocr = OcrCli()
    reviewer = OpenCodeReviewer(
        ladder, api_key_env="TEST_OCR_KEY", out_dir=tmp_path / "review", runner=ocr
    )
    monkeypatch.setenv("TEST_OCR_KEY", "test-key")

    result = asyncio.run(
        run_golden_path(
            "clean run test",
            repo=repo,
            runs_root=runs_root,
            selector=ladder,
            executor=executor,
            classifier=classifier,
            reviewer=reviewer,
            graph=GRAPH,
            policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
        )
    )
    assert result.outcome == RunOutcome.COMPLETE.value
    events = _load_events(result.run_dir)
    receipt = json.loads((result.run_dir / "receipt.json").read_text())
    return events, receipt, result.run_dir


class TestClaimsCleanRun:
    """A run without failure must mark failover/replacement claims NOT_OBSERVED."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events, self.receipt, self.run_dir = _clean_run(tmp_path, monkeypatch)
        self.claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        self.by_id = _claims_dict(self.claims)

    def test_failure_isolated_not_observed(self) -> None:
        assert self.by_id["failure_isolated"].status == CLAIM_STATUS_NOT_OBSERVED

    def test_automatic_failover_not_observed(self) -> None:
        assert self.by_id["automatic_failover"].status == CLAIM_STATUS_NOT_OBSERVED

    def test_cooldown_recorded_not_observed(self) -> None:
        assert self.by_id["cooldown_recorded"].status == CLAIM_STATUS_NOT_OBSERVED

    def test_replacement_completed_not_observed(self) -> None:
        assert self.by_id["replacement_completed"].status == CLAIM_STATUS_NOT_OBSERVED

    def test_validation_still_verified(self) -> None:
        assert self.by_id["validation_passed"].status == CLAIM_STATUS_VERIFIED

    def test_receipt_integrity_still_verified(self) -> None:
        assert self.by_id["receipt_integrity"].status == CLAIM_STATUS_VERIFIED

    def test_task_aware_selection_still_verified(self) -> None:
        assert self.by_id["task_aware_selection"].status == CLAIM_STATUS_VERIFIED


# ---------------------------------------------------------------------------
# Tampered receipt -> integrity CONTRADICTED
# ---------------------------------------------------------------------------


class TestClaimsTamperedReceipt:
    """Tampered receipt -> integrity CONTRADICTED."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events, self.receipt, self.run_dir = _clean_run(tmp_path, monkeypatch)
        # Tamper with the receipt file
        receipt_path = self.run_dir / "receipt.json"
        data = json.loads(receipt_path.read_text())
        data["goal"] = "TAMPERED GOAL"
        receipt_path.write_text(json.dumps(data))
        self.claims = derive_claims(self.events, self.receipt, run_dir=self.run_dir)
        self.by_id = _claims_dict(self.claims)

    def test_receipt_integrity_contradicted(self) -> None:
        c = self.by_id["receipt_integrity"]
        assert c.status == CLAIM_STATUS_CONTRADICTED
        assert len(c.evidence) > 0
        assert c.evidence[0].value["problems"]  # non-empty problems list


# ---------------------------------------------------------------------------
# Committed proof runs produce deterministic output
# ---------------------------------------------------------------------------


class TestClaimsCommittedProofRuns:
    """Committed proof runs produce deterministic claims output."""

    @pytest.mark.parametrize(
        "proof_dir",
        [
            "docs/proof/demo-run",
            "docs/proof/live-controller-run",
        ],
    )
    def test_proof_run_claims(self, proof_dir: str) -> None:
        run_dir = Path(proof_dir)
        if not (run_dir / "events.jsonl").exists():
            pytest.skip(f"{proof_dir} not present")
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
        receipt = json.loads((run_dir / "receipt.json").read_text())
        claims = derive_claims(events, receipt, run_dir=run_dir)

        # Basic structural assertions
        assert len(claims) == 12
        for c in claims:
            assert c.status in {CLAIM_STATUS_VERIFIED, CLAIM_STATUS_NOT_OBSERVED, CLAIM_STATUS_CONTRADICTED}
            d = c.to_dict()
            assert "id" in d and "text" in d and "status" in d and "evidence" in d

        # Determinism: running twice gives same result
        claims2 = derive_claims(events, receipt, run_dir=run_dir)
        for c1, c2 in zip(claims, claims2, strict=True):
            assert c1.id == c2.id
            assert c1.status == c2.status
            assert len(c1.evidence) == len(c2.evidence)

    @pytest.mark.parametrize(
        "proof_dir",
        [
            "docs/proof/harness-independence-2026-09-28/prime-run",
            "docs/proof/harness-independence-2026-09-28/direct-gateway-run",
        ],
    )
    def test_harness_independence_claims(self, proof_dir: str) -> None:
        run_dir = Path(proof_dir)
        if not (run_dir / "events.jsonl").exists():
            pytest.skip(f"{proof_dir} not present")
        events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines() if line.strip()]
        receipt = json.loads((run_dir / "receipt.json").read_text())
        claims = derive_claims(events, receipt, run_dir=run_dir)
        assert len(claims) == 12
        # Determinism
        claims2 = derive_claims(events, receipt, run_dir=run_dir)
        for c1, c2 in zip(claims, claims2, strict=True):
            assert c1.id == c2.id
            assert c1.status == c2.status
