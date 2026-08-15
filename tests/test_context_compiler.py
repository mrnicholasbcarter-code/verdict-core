"""ContextEnvelope + ContextCompiler tests (CONTEXT-001, #257)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from verdict.capability_passports import EvidenceAuthority
from verdict.context_envelope import (
    ITEM_AUTHORITY_UNCLASSIFIED,
    ContextCompiler,
    ContextEnvelope,
    ContextEnvelopeError,
    ContextItem,
    SourceRef,
)
from verdict.model_passports import ModelPassport
from verdict.models import ModelInfo, TaskSpec

TASK = TaskSpec(
    prompt="Implement failover transfer integrity",
    criticality="high",
    requirements=["preserve policy", "keep provenance"],
)


def _source(kind: str = "repo_file", ref: str = "verdict/context_envelope.py") -> SourceRef:
    return SourceRef(kind=kind, ref=ref, revision="abc123")


def _compiler() -> ContextCompiler:
    return ContextCompiler()


def _envelope(budget: int | None = None) -> ContextEnvelope:
    c = _compiler()
    return c.compile(
        TASK,
        task_id="task-1",
        budget=budget,
        sources={
            "policy_predicates": [
                c.item("policy", "protected work requires live availability truth", _source()),
                c.item(
                    "policy",
                    "cost must never rescue an ineligible candidate",
                    _source("adr", "docs/adr/0007.md"),
                ),
            ],
            "relevant_adrs": [
                c.item(
                    "adr",
                    "ADR-007: eligibility gate is fail-closed",
                    _source("adr", "docs/adr/0007.md"),
                ),
                c.item("adr", "ADR-012: passports never route", _source("adr", "docs/adr/0012.md")),
            ],
            "verified_decisions": [
                c.item(
                    "decision",
                    "quarantined candidates auto-recover",
                    SourceRef(kind="openviking", ref="ov://decisions/7"),
                )
            ],
            "artifacts": [
                c.item("artifact", "model-passports.v1.json", _source("git", "deadbeef"))
            ],
            "verification_requirements": [
                c.item("requirement", "uv run pytest -q", _source("manual", "CLAUDE.md"))
            ],
        },
    )


class TestContextEnvelopeContract:
    def test_all_seven_fields_present(self) -> None:
        envelope = _envelope()
        assert envelope.task_id == "task-1"
        assert envelope.goal.kind == "goal"
        assert len(envelope.policy_predicates) == 2
        assert len(envelope.relevant_adrs) == 2
        assert len(envelope.verified_decisions) == 1
        assert len(envelope.artifacts) == 1
        assert len(envelope.verification_requirements) == 1
        assert envelope.goal.content == TASK.prompt

    def test_every_item_has_source_metadata(self) -> None:
        for item in _envelope().iter_items():
            assert isinstance(item.source, SourceRef)
            assert item.source.kind in {
                "repo_file",
                "adr",
                "git",
                "openviking",
                "ruvector",
                "memory",
                "worker",
                "manual",
            }
            assert item.source.ref
            assert item.source.observed_at

    def test_strict_round_trip_no_loss(self) -> None:
        original = _envelope()
        restored = ContextEnvelope.from_dict(original.to_dict())
        assert restored == original
        assert restored.digest == original.digest

    def test_strict_round_trip_survives_rebind(self) -> None:
        # Simulate a failover: envelope is serialized on the old model and
        # reconstructed on the new one.
        wire = _envelope().to_dict()
        rebound = ContextEnvelope.from_dict(wire)
        assert rebound.goal.content == TASK.prompt
        assert len(rebound.policy_predicates) == 2
        assert all(item.source.ref for item in rebound.iter_items())

    def test_strict_mapping_rejects_unknown_field(self) -> None:
        payload = _envelope().to_dict()
        payload["mystery"] = True
        with pytest.raises(ContextEnvelopeError):
            ContextEnvelope.from_dict(payload)

    def test_strict_mapping_rejects_missing_field(self) -> None:
        payload = _envelope().to_dict()
        del payload["verification_requirements"]
        with pytest.raises(ContextEnvelopeError):
            ContextEnvelope.from_dict(payload)

    def test_item_kind_must_match_group(self) -> None:
        compiler = _compiler()
        with pytest.raises(ContextEnvelopeError):
            compiler.compile(
                TASK,
                sources={"policy_predicates": [compiler.item("artifact", "wrong kind", _source())]},
            )

    def test_source_kind_must_be_valid(self) -> None:
        with pytest.raises(ContextEnvelopeError):
            SourceRef(kind="mystery", ref="x")


class TestContextCompiler:
    def test_compile_infers_goal_from_prompt(self) -> None:
        envelope = _compiler().compile(TASK, task_id="task-x")
        assert envelope.goal.kind == "goal"
        assert envelope.goal.content == TASK.prompt
        assert envelope.goal.source.kind == "manual"

    def test_compile_aggregates_multiple_sources(self) -> None:
        envelope = _envelope()
        assert len(envelope.iter_items()) == 8  # goal + 2 + 2 + 1 + 1 + 1

    def test_optimize_fits_budget_keeps_policy(self) -> None:
        tight = _compiler().optimize_for(_envelope(), budget=80)
        assert tight.token_budget == 80
        assert tight.token_count <= 80
        # Policy predicates are non-negotiable.
        assert len(tight.policy_predicates) == 2
        # Drop list is recorded, not silent.
        assert tight.dropped_item_ids
        for item_id in tight.dropped_item_ids:
            assert all(i.item_id != item_id for i in tight.iter_items())

    def test_optimize_small_budget_keeps_only_goal_and_policy(self) -> None:
        # Tight budget that fits the goal plus both policy predicates but
        # nothing else: goal (14+8) + policy (19+8) + policy (16+8) == 73.
        optimized = _compiler().optimize_for(_envelope(), budget=73)
        assert optimized.token_count <= 73
        assert len(optimized.policy_predicates) == 2
        assert optimized.relevant_adrs == ()
        assert optimized.verified_decisions == ()
        assert optimized.artifacts == ()
        assert optimized.verification_requirements == ()

    def test_optimize_accepts_model_passport_window(self) -> None:
        passport = ModelPassport(
            provider="p", model_id="p/model", auth_state="authorized", context_window=80
        )
        optimized = _compiler().optimize_for(_envelope(), passport)
        assert optimized.token_budget == 80
        assert optimized.token_count <= 80

    def test_optimize_accepts_model_info_max_tokens(self) -> None:
        model = ModelInfo(id="p/model", provider="p", max_tokens=60)
        optimized = _compiler().optimize_for(_envelope(), budget=model.max_tokens)
        assert optimized.token_count <= 60

    def test_optimize_rejects_budget_below_goal(self) -> None:
        c = _compiler()
        envelope = c.compile(TASK, task_id="task-tiny")
        with pytest.raises(ContextEnvelopeError):
            c.optimize_for(envelope, budget=4)

    def test_optimize_without_drops_is_identity(self) -> None:
        envelope = _envelope(budget=10_000)
        optimized = _compiler().optimize_for(envelope, budget=10_000)
        assert optimized.dropped_item_ids == ()
        assert optimized.iter_items() == envelope.iter_items()


class TestContextAuthority:
    def test_authority_round_trips(self) -> None:
        item = ContextItem(
            item_id="auth-id",
            kind="adr",
            content="ADR-000",
            source=_source(),
            authority=EvidenceAuthority.VERIFIED.value,
        )
        restored = ContextItem.from_dict(item.to_dict())
        assert restored == item
        assert restored.authority == EvidenceAuthority.VERIFIED.value

    def test_default_authority_is_unclassified(self) -> None:
        item = _compiler().item("adr", "ADR-000", _source())
        assert item.authority == ITEM_AUTHORITY_UNCLASSIFIED
        # Missing authority on the wire falls back to unclassified.
        wire = item.to_dict()
        assert ContextItem.from_dict(wire) == item

    def test_authority_survives_optimize(self) -> None:
        item = ContextItem(
            item_id="auth-id",
            kind="policy",
            content="protected work requires live availability truth",
            source=_source(),
            authority=EvidenceAuthority.VERIFIED.value,
        )
        envelope = ContextEnvelope(
            task_id="t",
            goal=ContextItem("g", "goal", TASK.prompt, _source()),
            policy_predicates=(item,),
        )
        optimized = _compiler().optimize_for(envelope, budget=10_000)
        kept = [i for i in optimized.policy_predicates if i.item_id == "auth-id"]
        assert kept and kept[0].authority == EvidenceAuthority.VERIFIED.value

    def test_authority_affects_digest(self) -> None:
        src = SourceRef(kind="repo_file", ref="x", revision="abc123")
        # Same item content, only authority differs.
        plain = ContextItem("x", "adr", "ADR-000", src)
        versed = ContextItem("x", "adr", "ADR-000", src, authority=EvidenceAuthority.VERIFIED.value)
        assert _digest(plain.to_dict()) != _digest(versed.to_dict())


def _digest(value: Mapping[str, Any]) -> str:
    import hashlib
    import json

    canon = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"sha256:{hashlib.sha256(canon.encode()).hexdigest()}"
