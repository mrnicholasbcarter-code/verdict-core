"""Tests for verdict.orchestration.claims (BOD-279)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_flagship_failover_scenario import _run_scenario
from tests.test_orch_eligibility import FakeProbe
from verdict.orchestration.claims import (
    CLAIM_STATUS_CONTRADICTED,
    CLAIM_STATUS_NOT_OBSERVED,
    CLAIM_STATUS_VERIFIED,
    Claim,
    derive_claims,
)
from verdict.orchestration.contracts import RunOutcome
from verdict.orchestration.demo_scenario import (
    CONNECTIONS,
    GRAPH,
    INVENTORY,
    ROUTE_A,
    ROUTE_B,
    _init_repo,
    _load_events,
    _PassingOcrRunner,
    _worker_script,
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
            e
            for e in c.evidence
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

    def test_expected_verified_set(self) -> None:
        # The offline inventory uses distinct families and one capability-ineligible
        # route, so independent review and capability filtering are observed.
        verified = {c.id for c in self.claims if c.status == CLAIM_STATUS_VERIFIED}
        assert verified == {
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

    def test_offline_demo_verifies_independent_review(self) -> None:
        claim = self.by_id["independent_review"]
        assert claim.status == CLAIM_STATUS_VERIFIED
        value = claim.evidence[-1].value
        assert value["shared_identities"] == []
        assert value["shared_families"] == []
        assert "gamma/gemini-c" in value["reviewer_routes"]

    def test_offline_demo_verifies_capability_filtering(self) -> None:
        claim = self.by_id["capability_filtering"]
        assert claim.status == CLAIM_STATUS_VERIFIED
        reasons = claim.evidence[0].value["reasons"]
        assert "missing_capability:tools" in reasons or "insufficient_context" in reasons

    def test_claims_serialise_round_trip(self) -> None:
        for claim in self.claims:
            d = claim.to_dict()
            assert d["id"] == claim.id
            assert d["status"] == claim.status
            assert isinstance(d["evidence"], list)


# ---------------------------------------------------------------------------
# Clean run (no failure) — failover/replacement claims NOT_OBSERVED
# ---------------------------------------------------------------------------


def _clean_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    """Run the graph with NO faults — all nodes succeed first time."""
    repo = _init_repo(tmp_path)
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    state_path = tmp_path / "ladder-state.json"

    ladder = EligibilityLadder(
        INVENTORY, CONNECTIONS, FakeProbe(), state_path, allow_unknown_capacity=True
    )
    executor = ScriptedExecutor(_worker_script)  # No faults
    classifier = FailureIntelligence()

    ocr = _PassingOcrRunner()
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

    @pytest.mark.parametrize("proof_dir", ["docs/proof/demo-run", "docs/proof/live-controller-run"])
    def test_proof_run_claims(self, proof_dir: str) -> None:
        run_dir = Path(proof_dir)
        if not (run_dir / "events.jsonl").exists():
            pytest.skip(f"{proof_dir} not present")
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        receipt = json.loads((run_dir / "receipt.json").read_text())
        claims = derive_claims(events, receipt, run_dir=run_dir)

        # Basic structural assertions
        assert len(claims) == 12
        for c in claims:
            assert c.status in {
                CLAIM_STATUS_VERIFIED,
                CLAIM_STATUS_NOT_OBSERVED,
                CLAIM_STATUS_CONTRADICTED,
            }
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
        events = [
            json.loads(line)
            for line in (run_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        receipt = json.loads((run_dir / "receipt.json").read_text())
        claims = derive_claims(events, receipt, run_dir=run_dir)
        assert len(claims) == 12
        # Determinism
        claims2 = derive_claims(events, receipt, run_dir=run_dir)
        for c1, c2 in zip(claims, claims2, strict=True):
            assert c1.id == c2.id
            assert c1.status == c2.status


# ---------------------------------------------------------------------------
# Adversarial regressions (review of PR #720): a claim is VERIFIED only from
# evidence observed in THAT run; otherwise NOT_OBSERVED or CONTRADICTED.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _event(
    seq: int, type_: str, data: dict[str, Any] | None = None, node_id: str = "n"
) -> dict[str, Any]:
    return {
        "seq": seq,
        "at": "2026-09-28T00:00:00Z",
        "type": type_,
        "node_id": node_id,
        "data": data or {},
    }


def _get(
    name: str,
    events: list[dict[str, Any]] | None = None,
    receipt: dict[str, Any] | None = None,
    run_dir: Path | None = None,
) -> Claim:
    return _claims_dict(derive_claims(events or [], receipt or {}, run_dir=run_dir))[name]


_BAD = {CLAIM_STATUS_NOT_OBSERVED, CLAIM_STATUS_CONTRADICTED}


def test_task_aware_billing_metadata_only_not_verified() -> None:
    c = _get(
        "task_aware_selection",
        [_event(1, "selection", {"route_id": "p/model", "capacity_class": "subscription"})],
    )
    assert c.status == CLAIM_STATUS_NOT_OBSERVED


def test_task_aware_single_candidate_not_verified() -> None:
    evs = [
        _event(1, "understand", {"risk": "low"}, ""),
        _event(
            2, "plan_ready", {"nodes": [{"node_id": "n", "required_capabilities": ["tools"]}]}, ""
        ),
        _event(3, "eligibility", {"candidates": [{"route_id": "p/m", "rank": 0}]}),
        _event(4, "selection", {"route_id": "p/m", "rank": 0}),
    ]
    assert _get("task_aware_selection", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_capability_filtering_entitlement_rejection_not_verified() -> None:
    c = _get(
        "capability_filtering",
        [
            _event(
                1,
                "eligibility",
                {
                    "discovered": 2,
                    "entitled": 1,
                    "healthy": 1,
                    "available": 1,
                    "eligible": 1,
                    "rejections": [
                        {"route_id": "q/model", "stage": "ENTITLED", "reason": "no_active_account"}
                    ],
                },
            )
        ],
    )
    assert c.status == CLAIM_STATUS_NOT_OBSERVED


def test_capability_filtering_real_ladder_no_entitled_account(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from tests.test_orch_eligibility import row
    from verdict.orchestration.candidate_builder import build_rejections
    from verdict.orchestration.contracts import TaskRequirements
    from verdict.orchestration.runtime import _ladder_counts

    ladder = EligibilityLadder([row("p/model")], [], FakeProbe(), tmp_path / "state.json")
    verdicts = ladder.evaluate(TaskRequirements(), now=datetime.now(timezone.utc))
    data = {**_ladder_counts(verdicts), "rejections": build_rejections(verdicts)}
    assert (
        _get("capability_filtering", [_event(1, "eligibility", data)]).status
        == CLAIM_STATUS_NOT_OBSERVED
    )


def test_capability_filtering_task_eligible_rejection_verified() -> None:
    c = _get(
        "capability_filtering",
        [_event(1, "eligibility", {"rejections": {"TASK_ELIGIBLE": {"no_tools": 1}}})],
    )
    assert c.status == CLAIM_STATUS_VERIFIED


def test_health_unprobed_evaluate_summary_not_verified(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from tests.test_orch_eligibility import conn, row
    from verdict.orchestration.contracts import TaskRequirements
    from verdict.orchestration.runtime import _ladder_counts

    probe = FakeProbe()
    ladder = EligibilityLadder([row("p/model")], [conn("p")], probe, tmp_path / "state.json")
    verdicts = ladder.evaluate(TaskRequirements(), now=datetime.now(timezone.utc))
    assert probe.calls == []
    evs = [
        _event(1, "eligibility", {**_ladder_counts(verdicts), "probed": 0}),
        _event(2, "selection", {"route_id": "p/model"}),
    ]
    assert _get("health_considered", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_health_null_counter_not_verified() -> None:
    evs = [_event(1, "eligibility", {"healthy": None, "probed": 0, "probe": None})]
    assert _get("health_considered", evs).status == CLAIM_STATUS_NOT_OBSERVED


@pytest.mark.parametrize("route", ["router/x", "combo/x", "virtual/x", "auto/best-coding"])
def test_assignment_opaque_prefix_not_verified(route: str) -> None:
    assert _get("explicit_assignment", [_event(1, "dispatch", {"route_id": route})]).status in _BAD


def test_assignment_uses_production_prefixes() -> None:
    import verdict.orchestration.claims as claims_mod
    from verdict.orchestration.eligibility import _OPAQUE_PREFIXES

    assert claims_mod._OPAQUE_PREFIXES is _OPAQUE_PREFIXES


def test_failure_isolation_finish_precedes_failure() -> None:
    evs = [
        _event(1, "run_finished", {"outcome": "COMPLETE"}, ""),
        _event(2, "failure", {"category": "rate_limited"}),
    ]
    assert _get("failure_isolated", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_failure_isolation_controller_failure_only() -> None:
    evs = [
        _event(1, "failure", {"category": "controller_crash"}, ""),
        _event(2, "dispatch", {"route_id": "p/a"}),
        _event(3, "run_finished", {"outcome": "BLOCKED"}, ""),
    ]
    assert _get("failure_isolated", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_failure_isolation_no_continued_work() -> None:
    evs = [
        _event(1, "failure", {"category": "rate_limited"}),
        _event(2, "run_finished", {"outcome": "BLOCKED"}, ""),
    ]
    assert _get("failure_isolated", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_automatic_failover_exceeds_recorded_budget() -> None:
    evs = [
        _event(1, "run_started", {"retry_budget": {"max_attempts_per_node": 2}}, ""),
        _event(2, "failure", {"route_id": "p/a", "attempt": 4, "category": "rate_limited"}),
        _event(
            3,
            "reassign",
            {"from_route": "p/a", "to_route": "q/b", "reason": "rate_limited", "attempt": 5},
        ),
        _event(4, "terminal", {"attempt": 5, "ok": True, "reported_model": "q/b"}),
    ]
    assert (
        _get("automatic_failover", evs, {"retry_budget": {"max_attempts_per_node": 2}}).status
        in _BAD
    )


def test_automatic_failover_missing_budget() -> None:
    evs = [
        _event(1, "failure", {"attempt": 1}),
        _event(2, "reassign", {"from_route": "p/a", "to_route": "q/b", "attempt": 2}),
        _event(3, "terminal", {"attempt": 2, "ok": True, "reported_model": "q/b"}),
    ]
    assert _get("automatic_failover", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_automatic_failover_requires_prior_failure_and_ok_replacement() -> None:
    budget = _event(1, "run_started", {"retry_budget": {"max_attempts_per_node": 4}}, "")
    no_failure = [
        budget,
        _event(2, "reassign", {"attempt": 2}),
        _event(3, "terminal", {"attempt": 2, "ok": True}),
    ]
    assert _get("automatic_failover", no_failure).status == CLAIM_STATUS_NOT_OBSERVED
    failed_replacement = [
        budget,
        _event(2, "failure", {"attempt": 1}),
        _event(3, "reassign", {"attempt": 2}),
        _event(4, "terminal", {"attempt": 2, "ok": False}),
    ]
    assert _get("automatic_failover", failed_replacement).status == CLAIM_STATUS_NOT_OBSERVED


def test_cooldown_empty_marker() -> None:
    assert _get("cooldown_recorded", [_event(1, "cooldown")]).status == CLAIM_STATUS_NOT_OBSERVED


def test_cooldown_until_not_in_future() -> None:
    evs = [
        _event(
            1, "cooldown", {"key": "p", "scope": "provider", "until": "2026-09-27T00:00:00+00:00"}
        )
    ]
    assert _get("cooldown_recorded", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_context_unknown_second_hydration_not_verified() -> None:
    evs = [
        _event(1, "hydrate", {"budget_bytes": 100, "prompt_bytes": 90}),
        _event(2, "hydrate", {"budget_bytes": 100}, "second-node"),
    ]
    c = _get("context_within_budget", evs)
    assert c.status == CLAIM_STATUS_NOT_OBSERVED
    assert "only 1 of 2" in c.text


def test_context_string_byte_counts_not_verified() -> None:
    evs = [_event(1, "hydrate", {"budget_bytes": "90", "prompt_bytes": "100"})]
    assert _get("context_within_budget", evs).status == CLAIM_STATUS_NOT_OBSERVED


@pytest.mark.parametrize("data", [{"budget_bytes": 100}, {"prompt_bytes": 99}, {}])
def test_context_all_missing_not_observed(data: dict[str, Any]) -> None:
    assert (
        _get("context_within_budget", [_event(1, "hydrate", data)]).status
        == CLAIM_STATUS_NOT_OBSERVED
    )


def test_context_overflow_contradicted() -> None:
    evs = [_event(1, "hydrate", {"budget_bytes": 50, "prompt_bytes": 100})]
    assert _get("context_within_budget", evs).status == CLAIM_STATUS_CONTRADICTED


def test_replacement_success_before_failure() -> None:
    evs = [
        _event(1, "terminal", {"attempt": 1, "ok": True, "reported_model": "q/b"}),
        _event(2, "verify", {"ok": True, "command": "check"}),
        _event(3, "failure", {"attempt": 2, "route_id": "p/a"}),
    ]
    rec = {
        "nodes": [
            {
                "node_id": "n",
                "final_state": "VALIDATED",
                "attempts": [
                    {"attempt": 1, "route_id": "q/b", "outcome": "success"},
                    {"attempt": 2, "route_id": "p/a", "outcome": "failure"},
                ],
            }
        ]
    }
    assert _get("replacement_completed", evs, rec).status == CLAIM_STATUS_NOT_OBSERVED


def test_replacement_executed_on_failed_route() -> None:
    evs = [
        _event(1, "failure", {"attempt": 1, "route_id": "p/a"}),
        _event(2, "dispatch", {"attempt": 2, "route_id": "q/b"}),
        _event(
            3, "terminal", {"attempt": 2, "ok": True, "route_id": "q/b", "reported_model": "p/a"}
        ),
        _event(4, "verify", {"ok": True, "command": "check"}),
    ]
    assert _get("replacement_completed", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_replacement_receipt_only_not_verified() -> None:
    rec = {
        "nodes": [
            {
                "node_id": "n",
                "final_state": "VALIDATED",
                "attempts": [
                    {"attempt": 1, "route_id": "p/a", "outcome": "failure"},
                    {"attempt": 2, "route_id": "q/b", "outcome": "success"},
                ],
            }
        ]
    }
    assert _get("replacement_completed", receipt=rec).status == CLAIM_STATUS_NOT_OBSERVED


def test_validation_no_check_executed() -> None:
    evs = [_event(1, "verify", {"ok": True, "exit_code": 0, "command": "(none: non-code node)"})]
    assert _get("validation_passed", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_validation_later_failure_contradicted() -> None:
    evs = [
        _event(1, "verify", {"ok": True, "exit_code": 0, "command": "check"}),
        _event(2, "verify", {"ok": False, "exit_code": 1, "command": "check"}),
    ]
    assert _get("validation_passed", evs).status == CLAIM_STATUS_CONTRADICTED


def _review(route: str, blocking: int = 0, status: str = "PASS") -> dict[str, Any]:
    return _event(90, "review", {"status": status, "route_id": route, "blocking": blocking}, "")


def test_review_same_worker_route_attempts_missing() -> None:
    evs = [
        _event(1, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
        _review("kr/claude-sonnet-4"),
    ]
    rec = {"review": {"status": "PASS", "route_id": "kr/claude-sonnet-4"}, "nodes": []}
    assert _get("independent_review", evs, rec).status in _BAD


def test_review_same_executed_worker_identity() -> None:
    evs = [
        _event(1, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
        _event(2, "terminal", {"attempt": 1, "ok": True, "reported_model": "kr/gpt-5.6-terra"}),
        _review("kr/gpt-5.6-terra"),
    ]
    assert _get("independent_review", evs).status == CLAIM_STATUS_CONTRADICTED


def test_review_same_identity_in_earlier_attempt() -> None:
    evs = [
        _event(1, "dispatch", {"route_id": "kr/gpt-5.6-terra", "attempt": 1}),
        _event(2, "terminal", {"attempt": 1, "ok": False, "reported_model": "kr/gpt-5.6-terra"}),
        _event(3, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 2}),
        _event(4, "terminal", {"attempt": 2, "ok": True, "reported_model": "kr/claude-sonnet-4"}),
        _review("kr/gpt-5.6-terra"),
    ]
    assert _get("independent_review", evs).status == CLAIM_STATUS_CONTRADICTED


def test_review_pass_with_blocking_findings() -> None:
    evs = [
        _event(1, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
        _event(2, "terminal", {"attempt": 1, "ok": True, "reported_model": "kr/claude-sonnet-4"}),
        _review("kr/gpt-5.6-terra", blocking=1),
    ]
    assert _get("independent_review", evs).status in _BAD


def test_review_receipt_only_not_verified() -> None:
    rec = {
        "review": {"status": "PASS", "route_id": "kr/gpt-5.6-terra", "blocking": 0},
        "nodes": [
            {"node_id": "n", "attempts": [{"route_id": "kr/claude-sonnet-4", "outcome": "success"}]}
        ],
    }
    assert _get("independent_review", receipt=rec).status == CLAIM_STATUS_NOT_OBSERVED


def test_review_missing_executed_identity_not_observed() -> None:
    evs = [
        _event(1, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
        _event(2, "terminal", {"attempt": 1, "ok": True}),
        _review("kr/gpt-5.6-terra"),
    ]
    assert _get("independent_review", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_review_before_worker_terminal_not_verified() -> None:
    """A review recorded before the work it judges finished cannot certify that work."""
    review = _event(
        1, "review", {"status": "PASS", "route_id": "kr/gpt-5.6-terra", "blocking": 0}, ""
    )
    evs = [
        review,
        _event(2, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
        _event(3, "terminal", {"attempt": 1, "ok": True, "reported_model": "kr/claude-sonnet-4"}),
    ]
    assert _get("independent_review", evs).status == CLAIM_STATUS_NOT_OBSERVED


def test_review_observed_independent_verified() -> None:
    evs = [
        _event(1, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
        _event(2, "terminal", {"attempt": 1, "ok": True, "reported_model": "kr/claude-sonnet-4"}),
        _review("kr/gpt-5.6-terra"),
    ]
    assert _get("independent_review", evs).status == CLAIM_STATUS_VERIFIED


def test_review_antigravity_worker_agy_reviewer_not_independent() -> None:
    """agy and antigravity share google-antigravity; review is not independent."""
    evs = [
        _event(1, "dispatch", {"route_id": "agy/claude-sonnet-4-6", "attempt": 1}),
        _event(
            2, "terminal", {"attempt": 1, "ok": True, "reported_model": "agy/claude-sonnet-4-6"}
        ),
        _review("antigravity/gpt-oss-120b-medium"),
    ]
    claim = _get("independent_review", evs)
    assert claim.status in _BAD
    evidence = claim.evidence[0].value
    assert "google-antigravity" in evidence.get("shared_pools", [])


def test_receipt_integrity_unrelated_run_directory() -> None:
    demo = _REPO_ROOT / "docs/proof/demo-run"
    if not (demo / "events.jsonl").exists():
        pytest.skip("demo-run not present")
    evs = [_event(1, "run_started", {"run_id": "different-run"}, "")]
    c = _get("receipt_integrity", evs, {"run_id": "different-run", "goal": "tampered"}, demo)
    assert c.status == CLAIM_STATUS_CONTRADICTED


@pytest.mark.parametrize(
    "claim",
    [
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
    ],
)
def test_no_evidence_not_observed(claim: str) -> None:
    assert _get(claim).status == CLAIM_STATUS_NOT_OBSERVED


def _consistent_run(
    tmp_path: Path, records: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    from verdict.orchestration.contracts import NodeKind, WorkGraph, WorkNode
    from verdict.orchestration.receipt import EventLog, build_run_receipt, verify_run_receipt

    run_dir = tmp_path / "recorded-run"
    run_dir.mkdir()
    graph = WorkGraph(
        goal="claim adversarial recorded run",
        nodes=(
            WorkNode("n", "test", kind=NodeKind.RESEARCH, coding=False, required_capabilities=()),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl")
    for e in records:
        log.emit(e["type"], e["node_id"], **e["data"])
    receipt = build_run_receipt(run_dir)
    (run_dir / "receipt.json").write_text(json.dumps(receipt))
    assert verify_run_receipt(run_dir) == []
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return events, receipt, run_dir


def test_replacement_wrong_executed_route_with_valid_receipt(tmp_path: Path) -> None:
    evs, rec, run_dir = _consistent_run(
        tmp_path,
        [
            _event(1, "run_started", {"run_id": "recorded-run"}, ""),
            _event(2, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
            _event(
                3,
                "failure",
                {"route_id": "kr/claude-sonnet-4", "attempt": 1, "category": "rate_limited"},
            ),
            _event(4, "dispatch", {"route_id": "kr/gpt-5.6-terra", "attempt": 2}),
            _event(
                5,
                "terminal",
                {
                    "route_id": "kr/gpt-5.6-terra",
                    "reported_model": "kr/claude-sonnet-4",
                    "attempt": 2,
                    "ok": True,
                },
            ),
            _event(6, "verify", {"ok": True, "exit_code": 0, "command": "test"}),
            _event(7, "run_finished", {"outcome": "BLOCKED"}, ""),
        ],
    )
    assert _get("receipt_integrity", evs, rec, run_dir).status == CLAIM_STATUS_VERIFIED
    assert _get("replacement_completed", evs, rec, run_dir).status == CLAIM_STATUS_NOT_OBSERVED


def test_review_same_actual_worker_with_valid_receipt(tmp_path: Path) -> None:
    evs, rec, run_dir = _consistent_run(
        tmp_path,
        [
            _event(1, "run_started", {"run_id": "recorded-run"}, ""),
            _event(2, "dispatch", {"route_id": "kr/claude-sonnet-4", "attempt": 1}),
            _event(
                3,
                "terminal",
                {
                    "route_id": "kr/claude-sonnet-4",
                    "reported_model": "kr/gpt-5.6-terra",
                    "attempt": 1,
                    "ok": True,
                },
            ),
            _event(4, "verify", {"ok": True, "exit_code": 0, "command": "test"}),
            _event(
                5, "review", {"status": "PASS", "route_id": "kr/gpt-5.6-terra", "blocking": 0}, ""
            ),
            _event(6, "run_finished", {"outcome": "BLOCKED"}, ""),
        ],
    )
    assert _get("receipt_integrity", evs, rec, run_dir).status == CLAIM_STATUS_VERIFIED
    assert _get("independent_review", evs, rec, run_dir).status == CLAIM_STATUS_CONTRADICTED


def test_receipt_integrity_supplied_events_differ(tmp_path: Path) -> None:
    evs, rec, run_dir = _consistent_run(
        tmp_path,
        [
            _event(1, "run_started", {"run_id": "recorded-run"}, ""),
            _event(2, "run_finished", {"outcome": "BLOCKED"}, ""),
        ],
    )
    forged = [*evs, _event(3, "review", {"status": "PASS", "route_id": "x/y", "blocking": 0}, "")]
    assert _get("receipt_integrity", forged, rec, run_dir).status == CLAIM_STATUS_CONTRADICTED
