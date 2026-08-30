"""Orchestrate live catalog → classify → select → named check → receipt."""

from __future__ import annotations

from datetime import datetime, timezone

from verdict.live_routing import (
    GoldenPathReceipt,
    LiveRoutingError,
    LiveSurfaceBlocked,
    classify_identities,
    explain,
    failover_order,
    select_route,
    strip_secrets,
)
from verdict.live_routing_gateway import (
    DEFAULT_GATEWAY,
    execute_named_check,
    fetch_models,
    probe_identity,
)
from verdict.live_routing_usage import collect_usage


def run_live_routing(
    *,
    base_url: str = DEFAULT_GATEWAY,
    denylist: frozenset[str] = frozenset(),
    unit_id: str = "named-check",
    catalog_source: str = "live-gateway",
) -> dict[str, object]:
    if catalog_source != "live-gateway":
        raise LiveSurfaceBlocked("fixture catalog cannot emit a golden-path pass")
    identities, captured = fetch_models(base_url)
    try:
        usage = collect_usage()
    except Exception:
        usage = []
    candidates = classify_identities(identities, denylist=denylist, usage=usage)
    selection = select_route(candidates)
    explanation = strip_secrets(explain(candidates, selection))
    attempts: list[dict[str, object]] = []
    seen: set[str] = set()
    checker_passed = False
    chosen_id = selection.chosen.ref
    last_error = None
    for candidate in failover_order(selection):
        identity = candidate.identity
        if identity is None or identity.identity_id in seen:
            continue
        seen.add(identity.identity_id)
        if not probe_identity(base_url, identity.identity_id):
            attempts.append(
                {
                    "identity_id": identity.identity_id,
                    "cost_class": identity.cost_class,
                    "checker_passed": False,
                    "error": "health",
                }
            )
            continue
        try:
            passed, _body = execute_named_check(base_url, identity.identity_id)
        except LiveSurfaceBlocked as exc:
            last_error = exc.code
            attempts.append(
                {
                    "identity_id": identity.identity_id,
                    "cost_class": identity.cost_class,
                    "checker_passed": False,
                    "error": exc.code,
                }
            )
            break
        attempts.append(
            {
                "identity_id": identity.identity_id,
                "cost_class": identity.cost_class,
                "checker_passed": passed,
            }
        )
        if passed:
            checker_passed = True
            chosen_id = identity.identity_id
            break
    if not checker_passed and last_error == "live_surface_blocked":
        raise LiveSurfaceBlocked("named check could not execute on the live surface")
    if not checker_passed:
        raise LiveRoutingError(
            "exhausted", "no remaining qualified identity passed the named check"
        )
    receipt = GoldenPathReceipt(
        unit_id=unit_id,
        endpoint=base_url,
        chosen_id=chosen_id,
        paid_used=selection.paid_used,
        cheaper_available=selection.cheaper_available,
        attempts=attempts,
        checker_passed=True,
        catalog_captured_at=captured.isoformat(),
    )
    return {
        "explanation": explanation,
        "receipt": receipt.to_dict(),
        "captured_at": captured.isoformat(),
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
