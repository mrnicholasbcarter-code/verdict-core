"""BOD-144: RoutingReceiptV1 schema, digest, persistence, and redaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from verdict.receipt_store import ReceiptConflictError, ReceiptStore, redact_sensitive_dict
from verdict.routing_receipt import (
    REASON_IDENTITY_MISMATCH,
    REASON_PROBE_FAILED,
    REASON_SELECTED,
    ROUTING_RECEIPT_SCHEMA_VERSION,
    EvidenceValue,
    RouteRef,
    RoutingReceiptError,
    RoutingReceiptSchemaError,
    RoutingReceiptV1,
    attempt_scope,
    build_routing_receipt,
    decision_digest_for,
    finalize_routing_receipt,
    human_summary,
    link_recovery_attempt,
    load_routing_receipt,
    normalize_reason_code,
    persist_routing_receipt,
)


@dataclass(frozen=True)
class FakeDrop:
    model_id: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"model": self.model_id, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


def _pool() -> dict[str, Any]:
    return {
        "evidence_digest": "sha256:" + "d" * 64,
        "shortlist_digest": "sha256:" + "e" * 64,
        "shortlist": [
            {
                "route_id": "omniroute/gc/grok-4.6",
                "provider": "gc",
                "score": 0.9,
                "confidence": 0.8,
                "score_features": {"fit": 0.9},
                "inclusion_reason": "top",
                "metadata_id": "gc/grok-4.6",
                "family_key": "grok",
            }
        ],
        "hard_drops": [{"route_id": "other/x", "reason": "stale", "detail": "old"}],
        "probes": [
            {"route_id": "omniroute/gc/grok-4.6", "level": "cheap_health", "passed": True},
            {"route_id": "other/y", "level": "cheap_health", "passed": False, "detail": "timeout"},
        ],
    }


@dataclass(frozen=True)
class FakeAdmit:
    admitted: tuple[str, ...] = ("omniroute/gc/grok-4.6",)
    exclusions: tuple[FakeDrop, ...] = (
        FakeDrop("bad/model", "capability_mismatch", "no tools"),
    )
    chosen: str | None = "omniroute/gc/grok-4.6"
    empty_intersection: bool = False
    active_providers: tuple[str, ...] = ()
    free_tier_providers: tuple[str, ...] = ()
    pack_digest: str | None = "sha256:" + "a" * 64
    omissions: tuple[Any, ...] = ()
    included: tuple[Any, ...] = ()
    pack_state: str | None = None
    passport: tuple[Any, ...] = ({"provider": "gc", "ok": True},)
    confirm: tuple[Any, ...] = ({"latency_ms": 12, "ok": True},)
    selected_because: str | None = "cheap+capable"
    task_class: str | None = "coding"
    class_reasons: tuple[str, ...] = ("keywords",)
    requirements: tuple[str, ...] = ("tools",)
    capability_matches: tuple[Any, ...] = ()
    free_admitted: tuple[str, ...] = ()
    paid_admitted: tuple[str, ...] = ()
    task_complete: bool | None = True
    required_sources: tuple[str, ...] = ()
    prompt_digest: str | None = "sha256:" + "b" * 64
    capability_coverage: dict[str, Any] | None = None
    execution_attempts: tuple[Any, ...] = ()
    verification: dict[str, Any] | None = None
    receipt_id: str | None = None
    task_profile_digest: str | None = "sha256:" + "c" * 64
    spend_policy: str | None = "prefer_free"
    candidate_pool: dict[str, Any] | None = field(default_factory=_pool)
    context_plans: tuple[Any, ...] = ()


@dataclass(frozen=True)
class FakePlan:
    plan_id: str = "plan-1"
    candidate_id: str = "omniroute/gc/grok-4.6"
    token_budget: int = 4096
    output_token_reserve: int = 256
    tool_token_reserve: int = 128
    estimated_input_tokens: int | None = 900

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "token_budget": self.token_budget,
            "output_token_reserve": self.output_token_reserve,
            "tool_token_reserve": self.tool_token_reserve,
            "estimated_input_tokens": self.estimated_input_tokens,
        }

    @property
    def digest(self) -> str:
        return "sha256:" + "f" * 64


def _receipt(**kwargs: Any) -> RoutingReceiptV1:
    defaults: dict[str, Any] = {
        "admit": FakeAdmit(),
        "attempt_id": "a1",
        "story_id": "BOD-144",
        "work_unit_id": "u1",
        "selected_identity": {
            "gateway": "omniroute",
            "provider": "gc",
            "model": "gc/grok-4.6",
            "resource_pool": "default",
        },
        "requested_alias": "omniroute/auto/best-coding",
        "context_plan": FakePlan(),
    }
    defaults.update(kwargs)
    return build_routing_receipt(**defaults)


def test_schema_round_trip_and_digest_stability() -> None:
    receipt = _receipt()
    assert receipt.schema_version == ROUTING_RECEIPT_SCHEMA_VERSION
    assert receipt.decision_digest and receipt.decision_digest.startswith("sha256:")
    cloned = RoutingReceiptV1.from_dict(receipt.to_dict())
    assert cloned.decision_digest == receipt.decision_digest
    assert decision_digest_for(receipt.to_dict(include_digest=False)) == receipt.decision_digest


def test_unsupported_schema_refuses() -> None:
    receipt = _receipt()
    payload = receipt.to_dict()
    payload["schema_version"] = "routing-receipt/v9"
    with pytest.raises(RoutingReceiptSchemaError):
        RoutingReceiptV1.from_dict(payload)


def test_estimated_vs_observed_and_unknown() -> None:
    estimated = EvidenceValue.estimated(100, source="plan", unit="tokens")
    observed = EvidenceValue.observed(90, source="provider", unit="tokens")
    unknown = EvidenceValue.unknown(source="provider")
    assert estimated.to_dict()["kind"] == "estimated"
    assert observed.to_dict()["kind"] == "observed"
    assert unknown.to_dict()["value"] is None
    with pytest.raises(RoutingReceiptError):
        EvidenceValue(kind="unknown", value=0)
    with pytest.raises(RoutingReceiptError):
        EvidenceValue(kind="observed", value=None)


def test_reason_code_normalization() -> None:
    # Existing in-repo codes remain stable first-class values.
    assert normalize_reason_code("capability_mismatch") == "capability_mismatch"
    assert normalize_reason_code("stale") == "stale"
    assert normalize_reason_code("selected") == REASON_SELECTED
    # Legacy aliases only apply when the raw code is not already registered.
    assert normalize_reason_code("inactive_unconnected") == "inactive_unconnected"


def test_candidate_pipeline_preserves_exclusions_shortlist_and_probes() -> None:
    receipt = _receipt()
    by_id = {row.candidate_id: row for row in receipt.candidate_pipeline}
    assert "bad/model" in by_id
    assert "capability_mismatch" in by_id["bad/model"].reason_codes
    assert by_id["bad/model"].eligible is False
    chosen = by_id["omniroute/gc/grok-4.6"]
    assert chosen.shortlisted is True
    assert chosen.confirm is not None
    assert chosen.passport is not None
    assert REASON_SELECTED in chosen.reason_codes
    assert by_id["other/x"].eligible is False
    assert "stale" in by_id["other/x"].reason_codes


def test_digest_changes_when_selected_route_changes() -> None:
    first = _receipt()
    second = _receipt(
        selected_identity={
            "gateway": "omniroute",
            "provider": "gc",
            "model": "gc/grok-4.5",
            "resource_pool": "default",
        }
    )
    assert first.decision_digest != second.decision_digest


def test_identity_mismatch_is_receipt_visible() -> None:
    receipt = build_routing_receipt(
        admit=FakeAdmit(),
        attempt_id="a1",
        story_id="BOD-144",
        work_unit_id="u1",
        selected_identity={
            "gateway": "omniroute",
            "provider": "gc",
            "model": "gc/grok-4.6",
            "resource_pool": "default",
        },
        observed_identity={
            "gateway": "omniroute",
            "provider": "gc",
            "model": "gc/grok-4.5",
            "resource_pool": "default",
        },
    )
    assert receipt.execution.get("identity_mismatch") is True
    assert receipt.execution.get("failure_reason") == REASON_IDENTITY_MISMATCH
    assert receipt.state == "failed"


def test_context_plan_estimate_attached() -> None:
    receipt = _receipt()
    assert receipt.hydration["context_plan_digest"] == "sha256:" + "f" * 64
    budget = receipt.hydration["budget"]
    assert budget["estimated_input_tokens"]["kind"] == "estimated"
    assert budget["estimated_input_tokens"]["value"] == 900


def test_persist_finalize_idempotent_and_restart(tmp_path) -> None:
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path, strict_scope=True)
    receipt = _receipt()
    scope = attempt_scope(story_id="BOD-144", work_unit_id="u1", attempt_id="a1")
    persist_routing_receipt(store, receipt, scope=scope)
    # crash/restart
    store2 = ReceiptStore(path, strict_scope=True)
    loaded = load_routing_receipt(store2, scope=scope, attempt_id="a1")
    assert loaded is not None
    assert loaded.receipt_id == receipt.receipt_id
    assert loaded.state == "in_progress"
    fin = finalize_routing_receipt(
        store2,
        loaded,
        scope=scope,
        outcome="success",
        observed_identity={
            "gateway": "omniroute",
            "provider": "gc",
            "model": "gc/grok-4.6",
            "resource_pool": "default",
        },
        observed_usage={"input_tokens": 11, "latency_ms": 4},
        verification={"result": "pass", "head": "df148f2"},
    )
    assert fin.state == "finalized"
    # idempotent finalize
    fin2 = finalize_routing_receipt(
        store2,
        loaded,
        scope=scope,
        outcome="success",
        observed_identity={
            "gateway": "omniroute",
            "provider": "gc",
            "model": "gc/grok-4.6",
            "resource_pool": "default",
        },
        observed_usage={"input_tokens": 11, "latency_ms": 4},
        verification={"result": "pass", "head": "df148f2"},
    )
    assert fin2.state == "finalized"
    latest = load_routing_receipt(store2, scope=scope, attempt_id="a1")
    assert latest is not None
    assert latest.state == "finalized"
    assert latest.execution["usage"]["observed_input_tokens"]["kind"] == "observed"
    assert latest.execution["usage"]["observed_input_tokens"]["value"] == 11


def test_recovery_links_new_attempt_without_mutating_history(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db", strict_scope=True)
    receipt = _receipt()
    scope = attempt_scope(story_id="BOD-144", work_unit_id="u1", attempt_id="a1")
    persist_routing_receipt(store, receipt, scope=scope)
    fin = finalize_routing_receipt(store, receipt, scope=scope, outcome="failed")
    nxt = link_recovery_attempt(store, fin, new_attempt_id="a2", admit=FakeAdmit())
    assert nxt.attempt_id == "a2"
    assert nxt.parent_receipt_id == receipt.receipt_id
    assert nxt.final.get("recovery_of") == receipt.receipt_id
    prior = load_routing_receipt(store, scope=scope, attempt_id="a1")
    assert prior is not None
    assert prior.state == "failed"
    assert prior.decision_digest == fin.decision_digest


def test_conflicting_terminal_outcome_rejected(tmp_path) -> None:
    store = ReceiptStore(tmp_path / "receipts.db", strict_scope=True)
    receipt = _receipt()
    scope = attempt_scope(story_id="BOD-144", work_unit_id="u1", attempt_id="a1")
    persist_routing_receipt(store, receipt, scope=scope)
    finalize_routing_receipt(store, receipt, scope=scope, outcome="success")
    with pytest.raises(ReceiptConflictError):
        finalize_routing_receipt(store, receipt, scope=scope, outcome="failed")


def test_redaction_strips_secret_canaries() -> None:
    receipt = build_routing_receipt(
        admit=FakeAdmit(),
        attempt_id="a3",
        story_id="BOD-144",
        work_unit_id="u1",
        extensions={"note": "bearer SECRETTOKEN123 and password=hunter2"},
    )
    redacted = redact_sensitive_dict(receipt.to_dict())
    blob = str(redacted)
    assert "SECRETTOKEN123" not in blob
    assert "hunter2" not in blob


def test_route_ref_resource_pool_contract() -> None:
    ref = RouteRef(
        gateway="omniroute",
        provider="gc",
        model="gc/grok-4.6",
        credential_pool="pool-1",
    )
    payload = ref.to_dict()
    assert payload["resource_pool"] == "pool-1"
    assert "model" in payload


def test_human_summary_mentions_state() -> None:
    receipt = _receipt()
    text = human_summary(receipt)
    assert "routing-receipt" in text
    assert receipt.state in text


def test_probe_failed_reason_attached_when_probe_fails() -> None:
    pool = _pool()
    pool["probes"] = [
        {"route_id": "omniroute/gc/grok-4.6", "level": "cheap_health", "passed": False}
    ]
    admit = FakeAdmit(candidate_pool=pool)
    receipt = build_routing_receipt(
        admit=admit,
        attempt_id="a1",
        story_id="BOD-144",
        work_unit_id="u1",
    )
    row = next(r for r in receipt.candidate_pipeline if r.candidate_id == "omniroute/gc/grok-4.6")
    assert REASON_PROBE_FAILED in row.reason_codes
