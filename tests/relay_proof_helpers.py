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
            protocol=context.get("_relay_protocol")
            if isinstance(context, dict) and context.get("_relay_protocol") == "anthropic.messages"
            else None,
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


# --- signed worker assignments (serve-worker-assignment) -------------------------------------

#: The serve token and signing key used by the assignment tests.  The key is written to a temp
#: home only; no test touches the operator's ``~/.config/verdict``.
ASSIGNMENT_SERVE_TOKEN = "serve-token-0123456789"
ASSIGNMENT_KEY = bytes(range(32))


def configure_assignment_auth(monkeypatch: Any, home: Any) -> None:
    """Token-mode serve with the signing key in ``home``: the setup a real assignment needs.

    Overrides anonymous mode that other relay harnesses set.  ``HOME`` is redirected so the
    middleware reads the key from the temp directory.
    """
    from tests import worker_assignment_helpers as sign
    from verdict.worker_assignment import assignment_key_path

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    path = assignment_key_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sign.b64url(ASSIGNMENT_KEY), encoding="utf-8")
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", ASSIGNMENT_SERVE_TOKEN)
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)


def assignment_token(
    route_id: str, *, run_id: str = "run-1", node_id: str = "node-1", ttl: int = 600
) -> str:
    """A correctly signed assignment for ``route_id``, valid for ``ttl`` seconds from now."""
    import time

    from tests import worker_assignment_helpers as sign

    now = int(time.time())
    return sign.assignment(
        ASSIGNMENT_KEY, route_id=route_id, run_id=run_id, node_id=node_id, exp=now + ttl, iat=now
    )


def assignment_headers(route_id: str, **kwargs: Any) -> dict[str, str]:
    """The only credential a worker holds: the assignment as its bearer."""
    return {"authorization": f"Bearer {assignment_token(route_id, **kwargs)}"}


def serve_token_headers() -> dict[str, str]:
    return {"authorization": f"Bearer {ASSIGNMENT_SERVE_TOKEN}"}
