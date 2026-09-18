"""Best-of-admitted ranking on the serve cheap path.

Eligibility stays hard (free∩active ∩ fresh passport ∩ confirm). Ranking runs
only inside that admitted set: Core chooser owns the pick, named drops are
never re-admitted, and the receipt explains selected_because.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from verdict.admit_prove_confirm import ConfirmEvidence, PassportEvidence, gate_admit_prove_confirm
from verdict.chooser import apply_best_of_admitted, headroom_from_confirm_latency
from verdict.evidence import build_routing_decision_contract
from verdict.free_tier_admit import (
    NO_ELIGIBLE_TARGET,
    FreeTierAdmitReceipt,
    NamedDrop,
    admit_free_tier_active,
    snapshot_from_payloads,
)
from verdict.intelligence import IntelligenceService
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig

NOW = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)

GHOST = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
PROVEN = "opencode/hy3-free"
NAMED_DROP = "anthropic/claude-3-opus-20240229"


def _snapshot():
    return snapshot_from_payloads(
        catalog={
            "data": [
                {"id": GHOST, "owned_by": "openrouter"},
                {"id": PROVEN, "owned_by": "opencode"},
                {"id": NAMED_DROP, "owned_by": "claude"},
            ]
        },
        free_tier={
            "perModel": [
                {
                    "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                    "provider": "openrouter",
                    "freeType": "recurring-daily",
                },
                {"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"},
            ]
        },
        providers={
            "connections": [
                {"provider": "openrouter", "isActive": True, "testStatus": "active"},
                {"provider": "opencode", "isActive": True, "testStatus": "active"},
            ]
        },
    )


def _passport(identity_id: str, *, latency_p95: float | None = None) -> ModelPassport:
    qualified = NOW - timedelta(minutes=1)
    return ModelPassport(
        provider=identity_id.split("/", 1)[0],
        model_id=identity_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=NOW + timedelta(minutes=10),
        latency_p95=latency_p95,
    )


def _ok_transport():
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def _passport_evidence(identity_id: str, *, fresh: bool = True) -> PassportEvidence:
    return PassportEvidence(
        identity_id=identity_id,
        fresh=fresh,
        expires_at=(NOW + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        qualified_at=(NOW - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        auth_state="authorized",
        availability_state="eligible",
    )


def _confirm(identity_id: str, *, latency_ms: float, confirmed: bool = True) -> ConfirmEvidence:
    return ConfirmEvidence(
        identity_id=identity_id,
        confirmed=confirmed,
        status="confirmed" if confirmed else "failed",
        latency_ms=latency_ms,
    )


def _admitted_receipt(
    *, ghost_latency: float = 4000.0, proven_latency: float = 40.0, leak_drop: bool = False
) -> FreeTierAdmitReceipt:
    admitted = (GHOST, PROVEN, NAMED_DROP) if leak_drop else (GHOST, PROVEN)
    return FreeTierAdmitReceipt(
        admitted=admitted,
        exclusions=(NamedDrop(NAMED_DROP, "not_free_tier", "frontier is a named drop"),),
        chosen=GHOST,
        empty_intersection=False,
        active_providers=("openrouter", "opencode"),
        free_tier_providers=("openrouter", "opencode"),
        passport=(_passport_evidence(GHOST), _passport_evidence(PROVEN)),
        confirm=(
            _confirm(GHOST, latency_ms=ghost_latency),
            _confirm(PROVEN, latency_ms=proven_latency),
        ),
    )


def test_headroom_projection_keeps_unknown_unknown() -> None:
    assert headroom_from_confirm_latency(None) is None
    assert headroom_from_confirm_latency(0.0) == 100.0
    assert headroom_from_confirm_latency(5000.0) == 0.0


def test_selected_is_subset_of_passport_confirm_admitted() -> None:
    ranked = apply_best_of_admitted(_admitted_receipt(), now=NOW)
    assert ranked.chosen is not None
    assert ranked.chosen in ranked.admitted
    confirmed = {row.identity_id for row in ranked.confirm if row.confirmed}
    fresh = {row.identity_id for row in ranked.passport if row.fresh}
    assert ranked.chosen in confirmed
    assert ranked.chosen in fresh
    assert ranked.empty_intersection is False


def test_named_drop_never_selected_even_if_leaked_into_admitted() -> None:
    ranked = apply_best_of_admitted(_admitted_receipt(leak_drop=True), now=NOW)
    assert NAMED_DROP not in {ranked.chosen}
    assert ranked.chosen in {GHOST, PROVEN}
    assert any(item.model_id == NAMED_DROP for item in ranked.exclusions)


def test_receipt_includes_selected_because() -> None:
    ranked = apply_best_of_admitted(_admitted_receipt(), now=NOW)
    assert ranked.selected_because is not None
    assert ranked.selected_because.startswith("selected because")
    assert "admitted" in ranked.selected_because
    payload = ranked.to_dict()
    assert payload["selected_because"] == ranked.selected_because
    assert payload["chosen"] == ranked.chosen


def test_stronger_proven_beats_weaker_free_ghost_when_both_admitted() -> None:
    luck = admit_free_tier_active(_snapshot())
    assert luck.chosen == GHOST
    ranked = apply_best_of_admitted(_admitted_receipt(), now=NOW)
    assert ranked.chosen == PROVEN
    assert ranked.chosen != GHOST
    assert "last-confirm latency" in (ranked.selected_because or "")
    assert "named drops were not re-admitted" in (ranked.selected_because or "")


def test_gate_then_chooser_does_not_re_admit_named_drop() -> None:
    base = admit_free_tier_active(_snapshot())
    assert NAMED_DROP not in base.admitted
    gated = gate_admit_prove_confirm(
        base,
        passports={GHOST: _passport(GHOST), PROVEN: _passport(PROVEN)},
        confirm_transport=_ok_transport(),
        now=NOW,
    )
    assert NAMED_DROP not in gated.admitted
    with_latencies = replace(
        gated,
        confirm=tuple(
            replace(row, latency_ms=40.0 if row.identity_id == PROVEN else 4000.0)
            for row in gated.confirm
        ),
    )
    ranked = apply_best_of_admitted(
        with_latencies, passports={GHOST: _passport(GHOST), PROVEN: _passport(PROVEN)}, now=NOW
    )
    assert ranked.chosen == PROVEN
    assert ranked.chosen in ranked.admitted
    assert NAMED_DROP not in ranked.admitted
    assert ranked.selected_because is not None


def _service(snapshot, *, passports, confirm_transport) -> IntelligenceService:
    return IntelligenceService(
        primary_model=NAMED_DROP,
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        execute_offload=False,
        ruflo_command="nonexistent_ruflo",
        passports=passports,
        confirm_transport=confirm_transport,
        admit_now=NOW,
        context_roots=(),
        mcp_root="",
    )


def test_intelligence_cheap_path_chooser_owns_pick_and_stamps_receipt() -> None:
    snapshot = _snapshot()
    svc = _service(
        snapshot,
        passports={GHOST: _passport(GHOST), PROVEN: _passport(PROVEN)},
        confirm_transport=_ok_transport(),
    )
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    assert decision.decision == "selected"
    assert decision.model != NAMED_DROP
    assert decision.model != NO_ELIGIBLE_TARGET
    assert decision.admit_receipt is not None
    admitted = set(decision.admit_receipt["admitted"])
    assert decision.model in admitted
    assert NAMED_DROP not in admitted
    because = decision.admit_receipt["selected_because"]
    assert isinstance(because, str) and because.startswith("selected because")
    assert "chooser" in because.lower() or "admitted" in because
    assert "chooser_ranked_admitted" in decision.safety_flags
    assert decision.degraded_mode is False
    named = {item["reason"] for item in decision.admit_receipt["exclusions"]}
    assert "not_free_tier" in named or NAMED_DROP not in admitted


def test_best_of_admitted_selected_because_survives_evidence_embed() -> None:
    """Chooser-stamped selected_because must survive serve evidence compactification."""
    snapshot = _snapshot()
    svc = _service(
        snapshot,
        passports={GHOST: _passport(GHOST), PROVEN: _passport(PROVEN)},
        confirm_transport=_ok_transport(),
    )
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    pre_embed = decision.admit_receipt
    assert pre_embed is not None
    assert "selected_because" in pre_embed
    because = pre_embed["selected_because"]
    assert isinstance(because, str) and because
    payload = build_routing_decision_contract(
        decision,
        task="summarize this paragraph",
        criticality="low",
        features={"stream": False},
        correlation_id="corr-best-of-1",
        occurred_at="2026-09-18T00:00:00Z",
    ).to_dict()
    receipt = payload["receipt"]
    assert "selected_because" in receipt, (
        "selected_because present on in-memory admit receipt but dropped by evidence embed"
    )
    assert receipt["selected_because"] == because
    assert receipt.get("chooser_ranked_admitted") is True
    assert payload["selected_route"]["selected_because"] == because
