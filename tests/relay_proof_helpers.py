"""Shared relay fixture proof construction through the real passport gate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import verdict.api as api
from verdict.admit_prove_confirm import gate_admit_prove_confirm
from verdict.free_tier_admit import FreeTierAdmitReceipt
from verdict.intelligence import prepared_task_profile
from verdict.model_passports import ModelPassport
from verdict.models import RoutingDecision
from verdict.relay import admission_proof, dispatch_scope


def with_relay_proofs(
    decision: RoutingDecision, context: dict[str, Any] | None = None
) -> RoutingDecision:
    """Retain fixture selection but obtain proof exactly from trusted gate output.

    Real preparation uses this same passport gate and proof extractor. No fixture
    changes membership: only candidates already admitted by the decision qualify.
    """
    now = datetime.now(timezone.utc)
    models = [
        decision.model,
        *(
            item
            for item in decision.alternatives
            if any(
                row.get("model_id") == item and row.get("admitted") is True
                for row in decision.candidate_states
            )
        ),
    ]
    receipt = FreeTierAdmitReceipt(
        admitted=tuple(models),
        exclusions=(),
        chosen=decision.model,
        empty_intersection=False,
        active_providers=(decision.provider,),
        free_tier_providers=(),
    )
    passports = {
        model: ModelPassport(
            provider=decision.provider,
            model_id=model,
            auth_state="authorized",
            availability_state="eligible",
            qualified_at=now - timedelta(seconds=1),
            last_verified_timestamp=now - timedelta(seconds=1),
            expires_at=now + timedelta(seconds=300),
        )
        for model in models
    }
    confirmed = gate_admit_prove_confirm(receipt, passports=passports, now=now)
    confirmation = {
        "receipt_id": "fixture-confirm:"
        + hashlib.sha256(json.dumps(confirmed.to_dict(), sort_keys=True).encode()).hexdigest(),
        "admitted": list(confirmed.admitted),
        "passport": [row.to_dict() for row in confirmed.passport],
    }
    # Same profile the relay recomputes for the request it sends.
    digest = prepared_task_profile(api._task_text(context or {}), context or {})[1].digest
    proofs = {}
    for model in models:
        scope = dispatch_scope(
            gateway_origin=str(
                (context or {}).get("_dispatch_gateway_origin", "http://upstream.test/v1")
            ),
            provider=decision.provider,
            model=model,
            route_id=model,
            profile_digest=digest,
        )
        proof = admission_proof(
            confirmation,
            route_id=model,
            scope=scope,
            provider=decision.provider,
            model=model,
            authorization_digest="sha256:" + hashlib.sha256(model.encode()).hexdigest(),
            now=now,
        )
        assert proof is not None
        proofs[model] = proof
    return replace(decision, admission_proofs=proofs)
