"""Unit tests for the pure verified-model projection (BOD-291 unit 1).

Offline only: injected ``now``, no ``~/.verdict`` access, no network, no probes.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from verdict.orchestration.verified_models import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SCHEMA,
    EvidenceSnapshots,
    VerifiedModelQuery,
    VerifiedStatus,
    project_verified_models,
    snapshots_from_documents,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _default_owner(route_id: str) -> str:
    """Provider portion of a canonical route id (strip the gateway prefix)."""
    text = route_id
    if text.lower().startswith("omniroute/"):
        text = text[len("omniroute/") :]
    return text.split("/", 1)[0]


def row(
    route_id: str,
    *,
    owned_by: str | None = None,
    tools: bool | None = True,
    ctx: int = 200_000,
    **extra: Any,
) -> dict[str, Any]:
    caps: dict[str, Any] = {}
    if tools is not None:
        caps["tool_calling"] = tools
    r: dict[str, Any] = {
        "id": route_id,
        "owned_by": owned_by or _default_owner(route_id),
        "context_length": ctx,
        "capabilities": caps,
    }
    r.update(extra)
    return r


def conn(
    provider: str,
    *,
    active: bool = True,
    status: str = "ok",
    auth: str = "oauth",
    plan: str = "Max",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "isActive": active,
        "testStatus": status,
        "authType": auth,
        "plan_label": plan,
        **extra,
    }


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def at_rest_entry(
    route_id: str,
    *,
    checked_at: datetime,
    healthy: bool = True,
    category: str = "ok",
    chat_ok: bool = True,
    tool_ok: bool = True,
    identity: str = "verified",
    until: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "route_id": route_id,
        "category": category,
        "checked_at": iso(checked_at),
        "until": iso(until or checked_at),
        "consecutive_failures": 0,
        "chat_ok": chat_ok,
        "tool_ok": tool_ok,
        "healthy": healthy,
        "identity": identity,
    }
    entry.update(extra)
    return entry


def health_cache(
    *entries: dict[str, Any], cooldowns: dict[str, Any] | None = None
) -> dict[str, Any]:
    routes = {e["route_id"]: e for e in entries}
    doc: dict[str, Any] = {"schema_version": "1", "routes": routes}
    if cooldowns is not None:
        doc["cooldowns"] = cooldowns
    return doc


def project_one(
    inv: list[dict[str, Any]],
    conns: list[dict[str, Any]] | None,
    snaps: EvidenceSnapshots,
    *,
    now: datetime = NOW,
    query: VerifiedModelQuery | None = None,
):
    view = project_verified_models(inv, conns, snaps, now=now, query=query or VerifiedModelQuery())
    return view


def only_row(view):
    assert len(view.rows) == 1, [r.route_id for r in view.rows]
    return view.rows[0]


# ---------------------------------------------------------------------------
# All seven statuses
# ---------------------------------------------------------------------------


def test_verified_fresh_full_proof():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=60))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED
    assert r.coding_ok is True
    assert r.freshness == "fresh"
    assert r.identity == "verified"
    assert r.evidence_source == "health_cache"
    assert r.last_success_at == NOW - timedelta(seconds=60)


def test_stale_usable_between_600_and_1800():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=900))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.STALE
    assert r.freshness == "stale"
    assert r.coding_ok is False


def test_stale_expired_after_1800_retains_history():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=3600))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.STALE
    assert r.freshness == "expired"
    assert r.last_success_at == NOW - timedelta(seconds=3600)


def test_failed_route_negative():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(
                "cc/sonnet",
                checked_at=NOW - timedelta(seconds=30),
                healthy=False,
                category="timeout",
                chat_ok=False,
                tool_ok=False,
                identity="",
                until=NOW + timedelta(seconds=60),
                http_status=504,
            )
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.FAILED
    assert r.failure_category == "timeout"
    assert r.http_status == 504
    assert r.freshness == "negative"
    assert r.cooldown_until == NOW + timedelta(seconds=60)


def test_unavailable_auth_negative():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(
                "cc/sonnet",
                checked_at=NOW - timedelta(seconds=30),
                healthy=False,
                category="authentication",
                chat_ok=False,
                tool_ok=False,
                identity="",
                until=NOW + timedelta(hours=6),
                http_status=401,
            )
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_until == NOW + timedelta(hours=6)


def test_unverified_admitted_no_proof():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(now=NOW)  # no health evidence at all
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert r.reason in {"admitted_no_proof", "half_open"}


def test_inventory_only_opaque():
    inv = [row("omniroute/auto")]
    conns = [conn("auto")]
    snaps = snapshots_from_documents(now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.INVENTORY_ONLY
    assert r.restriction == "opaque_route"


def test_excluded_policy():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(policy_exclusions={"cc/sonnet": "operator blocked"}, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.EXCLUDED
    assert r.restriction == "policy_excluded"
    assert r.reason == "operator blocked"


# ---------------------------------------------------------------------------
# Boundary equality: 600s / 1800s / negative-until
# ---------------------------------------------------------------------------


def test_boundary_600s_exact_is_stale():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=600))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    # age >= 600 is STALE (design: VERIFIED is age < 600s)
    assert r.status is VerifiedStatus.STALE
    assert r.freshness == "stale"


def test_boundary_just_under_600s_is_verified():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=599))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED


def test_boundary_1800s_exact_is_expired():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=1800))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.STALE
    assert r.freshness == "expired"


def test_boundary_just_under_1800s_is_usable_stale():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=1799))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.freshness == "stale"


def test_negative_until_equality_is_expired_half_open():
    # until == now: the negative is expired -> not FAILED, route is half-open UNVERIFIED.
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(
                "cc/sonnet",
                checked_at=NOW - timedelta(seconds=120),
                healthy=False,
                category="timeout",
                chat_ok=False,
                tool_ok=False,
                identity="",
                until=NOW,
            )
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert "half_open" in r.restrictions


def test_negative_until_one_second_future_is_failed():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(
                "cc/sonnet",
                checked_at=NOW - timedelta(seconds=120),
                healthy=False,
                category="timeout",
                chat_ok=False,
                tool_ok=False,
                identity="",
                until=NOW + timedelta(seconds=1),
            )
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.FAILED


# ---------------------------------------------------------------------------
# Identity verified vs not_reported
# ---------------------------------------------------------------------------


def test_identity_not_reported_is_unverified_even_if_http200():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(
                "cc/sonnet",
                checked_at=NOW - timedelta(seconds=30),
                identity="not_reported",
                http_status=200,
            )
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert r.reason == "identity_not_verified"
    assert "identity_not_verified" in r.restrictions


def test_identity_verified_mints_verified():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30), identity="verified")
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED


# ---------------------------------------------------------------------------
# chat-only vs tool_ok; tools False capability
# ---------------------------------------------------------------------------


def test_chat_only_verified_but_not_coding():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30), tool_ok=False)
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED
    assert r.coding_ok is False
    assert "chat_only_not_coding_verified" in r.restrictions


def test_tool_ok_true_but_tools_capability_false_blocks_coding():
    inv = [row("omniroute/cc/sonnet", tools=False)]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30), tool_ok=True)
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED  # chat-only route still VERIFIED, not excluded
    assert r.coding_ok is False
    assert r.tools is False


def test_tools_null_capability_allows_coding_with_tool_proof():
    inv = [row("omniroute/cc/sonnet", tools=None)]  # capabilities has no tool_calling key
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30), tool_ok=True)
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED
    assert r.coding_ok is True
    assert r.tools is None


# ---------------------------------------------------------------------------
# Conflicts across stores
# ---------------------------------------------------------------------------


def test_at_rest_positive_but_active_route_negative_elsewhere_fails_closed():
    # at-rest positive, but worker cache has an active diagnostic negative for the
    # same route. A positive from any store cannot override an active negative.
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    worker = {
        "cc/sonnet": {
            "healthy": False,
            "category": "upstream",
            "status_code": 503,
            "observed_at": iso(NOW - timedelta(seconds=10)),
            "expires_at": iso(NOW + timedelta(seconds=120)),
        }
    }
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        worker_health_doc=worker,
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.FAILED
    assert r.failure_category == "upstream"


def test_ladder_positive_only_is_unverified_hint():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    ladder = {
        "health": {
            "cc/sonnet": {
                "healthy": True,
                "category": "ok",
                "checked_at": iso(NOW - timedelta(seconds=60)),
            }
        }
    }
    snaps = snapshots_from_documents(ladder_state_doc=ladder, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert "ladder_positive_hint" in r.hints


def test_worker_positive_only_is_unverified_hint():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    worker = {
        "cc/sonnet": {
            "healthy": True,
            "category": "ok",
            "observed_at": iso(NOW),
            "expires_at": iso(NOW + timedelta(seconds=120)),
        }
    }
    snaps = snapshots_from_documents(worker_health_doc=worker, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert "worker_positive_hint" in r.hints


def test_provider_cooldown_beats_route_positive():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    ladder = {
        "cooldowns": {
            "provider:cc": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=300))}
        }
    }
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        ladder_state_doc=ladder,
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope == "provider:cc"


def test_expired_cooldown_ignored_positive_wins():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    ladder = {
        "cooldowns": {
            "provider:cc": {"category": "rate_limited", "until": iso(NOW - timedelta(seconds=1))}
        }
    }
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        ladder_state_doc=ladder,
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED


def test_provider_cooldown_does_not_sink_sibling_provider():
    inv = [row("omniroute/cc/sonnet"), row("omniroute/kr/opus", owned_by="kr")]
    conns = [conn("cc"), conn("kr")]
    ladder = {
        "cooldowns": {
            "provider:cc": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=300))}
        }
    }
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30)),
            at_rest_entry("kr/opus", checked_at=NOW - timedelta(seconds=30)),
        ),
        ladder_state_doc=ladder,
        now=NOW,
    )
    view = project_one(inv, conns, snaps)
    by_id = {r.route_id: r for r in view.rows}
    assert by_id["cc/sonnet"].status is VerifiedStatus.UNAVAILABLE
    assert by_id["kr/opus"].status is VerifiedStatus.VERIFIED


# ---------------------------------------------------------------------------
# Malformed / future / schema / key mismatch -> source_errors, never healthy
# ---------------------------------------------------------------------------


def test_wrong_schema_version_records_error_and_no_proof():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    hc = {
        "schema_version": "2",
        "routes": {"cc/sonnet": at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))},
    }
    snaps = snapshots_from_documents(health_cache_doc=hc, now=NOW)
    view = project_one(inv, conns, snaps)
    assert any("schema_version" in e for e in view.source_errors)
    assert only_row(view).status is not VerifiedStatus.VERIFIED


def test_key_mismatch_records_error_never_healthy():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    # map key cc/sonnet but route_id says cc/other
    hc = {
        "schema_version": "1",
        "routes": {"cc/sonnet": at_rest_entry("cc/other", checked_at=NOW - timedelta(seconds=30))},
    }
    snaps = snapshots_from_documents(health_cache_doc=hc, now=NOW)
    view = project_one(inv, conns, snaps)
    assert any("does not match map key" in e for e in view.source_errors)
    assert only_row(view).status is not VerifiedStatus.VERIFIED


def test_future_checked_at_records_error_never_healthy():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    hc = health_cache(at_rest_entry("cc/sonnet", checked_at=NOW + timedelta(seconds=60)))
    snaps = snapshots_from_documents(health_cache_doc=hc, now=NOW)
    view = project_one(inv, conns, snaps)
    assert any("future" in e for e in view.source_errors)
    assert only_row(view).status is not VerifiedStatus.VERIFIED


def test_non_literal_bool_rejected():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    entry = at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
    entry["healthy"] = "true"  # not a literal bool
    hc = {"schema_version": "1", "routes": {"cc/sonnet": entry}}
    snaps = snapshots_from_documents(health_cache_doc=hc, now=NOW)
    view = project_one(inv, conns, snaps)
    assert any("literal boolean" in e for e in view.source_errors)
    assert only_row(view).status is not VerifiedStatus.VERIFIED


def test_naive_timestamp_rejected():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    entry = at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
    entry["checked_at"] = "2026-09-26T11:59:00"  # naive, no tz
    hc = {"schema_version": "1", "routes": {"cc/sonnet": entry}}
    snaps = snapshots_from_documents(health_cache_doc=hc, now=NOW)
    view = project_one(inv, conns, snaps)
    assert view.source_errors
    assert only_row(view).status is not VerifiedStatus.VERIFIED


def test_unknown_category_rejected():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    entry = at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30), category="weird")
    hc = {"schema_version": "1", "routes": {"cc/sonnet": entry}}
    snaps = snapshots_from_documents(health_cache_doc=hc, now=NOW)
    view = project_one(inv, conns, snaps)
    assert any("category" in e for e in view.source_errors)
    assert only_row(view).status is not VerifiedStatus.VERIFIED


def test_malformed_source_keeps_independent_blocker():
    # health cache malformed, but an independent provider cooldown still applies.
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    hc = {"schema_version": "9", "routes": {}}
    ladder = {
        "cooldowns": {
            "provider:cc": {"category": "authentication", "until": iso(NOW + timedelta(hours=6))}
        }
    }
    snaps = snapshots_from_documents(health_cache_doc=hc, ladder_state_doc=ladder, now=NOW)
    view = project_one(inv, conns, snaps)
    assert view.source_errors
    assert only_row(view).status is VerifiedStatus.UNAVAILABLE


def test_missing_at_rest_file_is_normal_not_verified():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(health_cache_doc=None, now=NOW)
    assert not snaps.source_errors
    assert only_row(project_one(inv, conns, snaps)).status is VerifiedStatus.UNVERIFIED


# ---------------------------------------------------------------------------
# Dedupe and contradictory duplicates
# ---------------------------------------------------------------------------


def test_duplicate_canonical_rows_collapse_to_one():
    inv = [row("omniroute/cc/sonnet"), row("cc/sonnet")]  # same canonical id
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        now=NOW,
    )
    view = project_one(inv, conns, snaps)
    assert view.total_count == 1


def test_contradictory_duplicates_fail_closed():
    inv = [row("omniroute/cc/sonnet", tools=True), row("cc/sonnet", tools=False)]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(now=NOW)
    view = project_one(inv, conns, snaps)
    r = only_row(view)
    assert r.status is VerifiedStatus.INVENTORY_ONLY
    assert r.restriction == "contradictory_inventory"
    assert any("contradictory" in e for e in view.source_errors)


# ---------------------------------------------------------------------------
# Filters / paging / counts reconciliation
# ---------------------------------------------------------------------------


def _many(n: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], EvidenceSnapshots]:
    inv = [row(f"omniroute/cc/m{i:04d}") for i in range(n)]
    conns = [conn("cc")]
    entries = [
        at_rest_entry(f"cc/m{i:04d}", checked_at=NOW - timedelta(seconds=30)) for i in range(n)
    ]
    snaps = snapshots_from_documents(health_cache_doc=health_cache(*entries), now=NOW)
    return inv, conns, snaps


def test_counts_reconcile_to_total_before_paging():
    inv, conns, snaps = _many(120)
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(page_size=50))
    assert view.total_count == 120
    assert sum(view.counts_by_status.values()) == 120
    assert len(view.rows) == 50
    assert view.page_count == 3


def test_filtered_counts_reconcile_to_filtered_count():
    inv = [row("omniroute/cc/sonnet"), row("omniroute/kr/opus", owned_by="kr")]
    conns = [conn("cc"), conn("kr")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        now=NOW,
    )
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(provider="cc"))
    assert view.total_count == 2
    assert view.filtered_count == 1
    assert sum(view.filtered_counts_by_status.values()) == 1
    assert only_row(view).route_id == "cc/sonnet"


def test_status_filter():
    inv = [row("omniroute/cc/sonnet"), row("omniroute/cc/haiku")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30)),
            at_rest_entry("cc/haiku", checked_at=NOW - timedelta(seconds=900)),
        ),
        now=NOW,
    )
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(status="STALE"))
    assert view.filtered_count == 1
    assert only_row(view).route_id == "cc/haiku"


def test_search_case_insensitive_over_route_provider_reason():
    inv = [row("omniroute/cc/sonnet"), row("omniroute/kr/opus", owned_by="kr")]
    conns = [conn("cc"), conn("kr")]
    snaps = snapshots_from_documents(now=NOW)
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(search="SONNET"))
    assert view.filtered_count == 1
    assert only_row(view).route_id == "cc/sonnet"


def test_out_of_range_page_clamps_to_last():
    inv, conns, snaps = _many(120)
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(page=99, page_size=50))
    assert view.page == 3
    assert len(view.rows) == 20


def test_empty_result_is_page_1_page_count_0():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(now=NOW)
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(status="VERIFIED"))
    assert view.filtered_count == 0
    assert view.page == 1
    assert view.page_count == 0
    assert view.rows == ()


def test_page_size_over_cap_clamped_to_200():
    inv, conns, snaps = _many(10)
    view = project_one(inv, conns, snaps, query=VerifiedModelQuery(page_size=5000))
    assert view.page_size == MAX_PAGE_SIZE


def test_default_page_size_is_50():
    inv, conns, snaps = _many(120)
    view = project_one(inv, conns, snaps)
    assert view.page_size == DEFAULT_PAGE_SIZE == 50


def test_invalid_status_filter_raises():
    inv = [row("omniroute/cc/sonnet")]
    with pytest.raises(ValueError):
        project_verified_models(
            inv,
            [conn("cc")],
            snapshots_from_documents(now=NOW),
            now=NOW,
            query=VerifiedModelQuery(status="BOGUS"),
        )


def test_invalid_page_raises():
    inv = [row("omniroute/cc/sonnet")]
    with pytest.raises(ValueError):
        project_verified_models(
            inv,
            [conn("cc")],
            snapshots_from_documents(now=NOW),
            now=NOW,
            query=VerifiedModelQuery(page=0),
        )


def test_invalid_page_size_raises():
    inv = [row("omniroute/cc/sonnet")]
    with pytest.raises(ValueError):
        project_verified_models(
            inv,
            [conn("cc")],
            snapshots_from_documents(now=NOW),
            now=NOW,
            query=VerifiedModelQuery(page_size=0),
        )


def test_naive_now_rejected():
    inv = [row("omniroute/cc/sonnet")]
    with pytest.raises(ValueError):
        project_verified_models(
            inv, [conn("cc")], snapshots_from_documents(now=NOW), now=datetime(2026, 9, 26, 12, 0)
        )


# ---------------------------------------------------------------------------
# JSON schema keys stable; sort order
# ---------------------------------------------------------------------------


_ROW_KEYS = {
    "route_id",
    "provider",
    "status",
    "coding_ok",
    "last_success_at",
    "checked_at",
    "evidence_source",
    "fresh_until",
    "expires_at",
    "freshness",
    "capabilities",
    "restriction",
    "reason",
    "restrictions",
    "cooldown_until",
    "cooldown_scope",
    "failure_category",
    "http_status",
    "latency_ms",
    "identity",
    "probe_class",
    "agentic_ok",
    "agentic_checked_at",
    "capacity_class",
    "refreshable",
    "refresh_reason",
    "hints",
}
_ENVELOPE_KEYS = {
    "schema",
    "generated_at",
    "rows",
    "counts_by_status",
    "filtered_counts_by_status",
    "total_count",
    "filtered_count",
    "filters",
    "page",
    "page_size",
    "page_count",
    "source_errors",
}


def test_envelope_schema_keys_stable():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        now=NOW,
    )
    d = project_one(inv, conns, snaps).to_dict()
    assert set(d) == _ENVELOPE_KEYS
    assert d["schema"] == SCHEMA
    assert set(d["counts_by_status"]) == {s.value for s in VerifiedStatus}
    assert set(d["filters"]) == {"status", "provider", "search"}


def test_row_schema_keys_stable_and_unknown_is_null():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(now=NOW)  # UNVERIFIED, lots of unknown
    d = project_one(inv, conns, snaps).to_dict()
    r = d["rows"][0]
    assert set(r) == _ROW_KEYS
    # unknown is null, never zero / false substitution
    assert r["last_success_at"] is None
    assert r["http_status"] is None
    assert r["failure_category"] is None
    assert set(r["capabilities"]) == {"context_window", "tools", "structured"}


def test_timestamps_are_z_suffixed():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        now=NOW,
    )
    d = project_one(inv, conns, snaps).to_dict()
    assert d["generated_at"].endswith("Z")
    assert d["rows"][0]["checked_at"].endswith("Z")


def test_sort_order_by_status_then_recency():
    inv = [row("omniroute/cc/a"), row("omniroute/cc/b"), row("omniroute/cc/fail")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/a", checked_at=NOW - timedelta(seconds=120)),
            at_rest_entry("cc/b", checked_at=NOW - timedelta(seconds=30)),
            at_rest_entry(
                "cc/fail",
                checked_at=NOW - timedelta(seconds=30),
                healthy=False,
                category="timeout",
                chat_ok=False,
                tool_ok=False,
                identity="",
                until=NOW + timedelta(seconds=60),
            ),
        ),
        now=NOW,
    )
    rows = project_one(inv, conns, snaps).rows
    statuses = [r.status for r in rows]
    assert statuses == [VerifiedStatus.VERIFIED, VerifiedStatus.VERIFIED, VerifiedStatus.FAILED]
    # within VERIFIED newest last_success_at first
    assert rows[0].route_id == "cc/b"
    assert rows[1].route_id == "cc/a"


# ---------------------------------------------------------------------------
# No secrets in output
# ---------------------------------------------------------------------------


def test_no_secret_from_connection_field_in_output():
    secret = "sk-SUPERSECRETKEY1234567890"
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc", api_key=secret, authorization=f"Bearer {secret}")]
    snaps = snapshots_from_documents(
        policy_exclusions={"cc/sonnet": f"blocked api_key={secret}"}, now=NOW
    )
    import json as _json

    d = project_one(inv, conns, snaps).to_dict()
    blob = _json.dumps(d)
    assert secret not in blob


def test_no_secret_from_policy_reason_sanitized():
    secret = "sk-ABCDEF1234567890TOKEN"
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        policy_exclusions={"cc/sonnet": f"token={secret} denied"}, now=NOW
    )
    r = only_row(project_one(inv, conns, snaps))
    assert secret not in (r.reason or "")


# ---------------------------------------------------------------------------
# Capacity class / refresh eligibility
# ---------------------------------------------------------------------------


def test_prepaid_stale_is_refreshable():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc", auth="oauth", plan="Max")]  # subscription
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=900))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.capacity_class == "subscription"
    assert r.status is VerifiedStatus.STALE
    assert r.refreshable is True
    assert r.refresh_reason is None


def test_metered_unverified_requires_confirmation_not_auto():
    inv = [row("omniroute/ap/gpt", owned_by="ap", pricing={"input": 1.0, "output": 2.0})]
    conns = [conn("ap", auth="apikey", plan="PAYG")]
    snaps = snapshots_from_documents(now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.capacity_class == "metered"
    assert r.status is VerifiedStatus.UNVERIFIED
    assert r.refreshable is False
    assert r.refresh_reason == "requires_confirmation"


def test_verified_fresh_not_refreshable_no_reason():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30))
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED
    assert r.refreshable is False


# ---------------------------------------------------------------------------
# Admission facts: precomputed vs conservative derivation
# ---------------------------------------------------------------------------


def test_precomputed_admission_fact_not_admitted_is_inventory_only():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    facts = {
        "cc/sonnet": {"admitted": False, "first_failed_stage": "POLICY", "reason": "deny_rule"}
    }
    snaps = snapshots_from_documents(admission_facts=facts, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.INVENTORY_ONLY
    assert r.reason == "deny_rule"


def test_precomputed_admission_fact_entitled_drop_is_unavailable():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    facts = {
        "cc/sonnet": {
            "admitted": False,
            "first_failed_stage": "ENTITLED",
            "reason": "no_active_account",
        }
    }
    snaps = snapshots_from_documents(admission_facts=facts, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE


def test_conservative_no_connection_is_unavailable():
    inv = [row("omniroute/cc/sonnet")]
    conns: list[dict[str, Any]] = []  # no connection rows at all
    snaps = snapshots_from_documents(now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.reason == "no_connection_evidence"


def test_inactive_account_is_unavailable():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc", active=False)]
    snaps = snapshots_from_documents(now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.reason == "no_active_account"


def test_bad_connection_status_is_unavailable():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc", status="unauthorized")]
    snaps = snapshots_from_documents(now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.restriction.startswith("connection_status:")


def test_missing_admission_fact_for_route_is_non_admissible():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    facts = {"cc/other": {"admitted": True}}  # provided, but not for this route
    snaps = snapshots_from_documents(admission_facts=facts, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status in {VerifiedStatus.INVENTORY_ONLY, VerifiedStatus.UNAVAILABLE}


# ---------------------------------------------------------------------------
# Half-open expired negative, absent cache -> UNVERIFIED hints
# ---------------------------------------------------------------------------


def test_expired_negative_is_half_open_unverified():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(
                "cc/sonnet",
                checked_at=NOW - timedelta(seconds=200),
                healthy=False,
                category="timeout",
                chat_ok=False,
                tool_ok=False,
                identity="",
                until=NOW - timedelta(seconds=10),
            )
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert "half_open" in r.restrictions


# ---------------------------------------------------------------------------
# Scoped cooldown category tie-break (auth beats rate-limit)
# ---------------------------------------------------------------------------


def test_availability_category_order_auth_before_ratelimit():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    ladder = {
        "cooldowns": {
            "provider:cc": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=300))}
        }
    }
    at_rest_cd = {
        "provider:cc": {"category": "authentication", "until": iso(NOW + timedelta(hours=6))}
    }
    snaps = snapshots_from_documents(
        health_cache_doc={"schema_version": "1", "routes": {}, "cooldowns": at_rest_cd},
        ladder_state_doc=ladder,
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.reason == "authentication"


# ---------------------------------------------------------------------------
# Performance: 7,000-row fixture < 2s
# ---------------------------------------------------------------------------


def test_7000_rows_projects_under_2s():
    n = 7000
    inv = [row(f"omniroute/cc/m{i:05d}") for i in range(n)]
    conns = [conn("cc")]
    entries = [
        at_rest_entry(f"cc/m{i:05d}", checked_at=NOW - timedelta(seconds=30)) for i in range(n)
    ]
    snaps = snapshots_from_documents(health_cache_doc=health_cache(*entries), now=NOW)
    start = time.perf_counter()
    view = project_verified_models(
        inv, conns, snaps, now=NOW, query=VerifiedModelQuery(page_size=50)
    )
    elapsed = time.perf_counter() - start
    assert view.total_count == n
    assert len(view.rows) == 50
    assert elapsed < 2.0, f"projection took {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Out-of-inventory evidence orphan is not a candidate
# ---------------------------------------------------------------------------


def test_evidence_only_orphan_route_not_projected():
    inv = [row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=30)),
            at_rest_entry("cc/ghost", checked_at=NOW - timedelta(seconds=30)),
        ),
        now=NOW,
    )
    view = project_one(inv, conns, snaps)
    ids = {r.route_id for r in view.rows}
    assert ids == {"cc/sonnet"}


# ---------------------------------------------------------------------------
# Independent-review regression tests (cx/gpt-6-sol REQUEST_CHANGES).
# Each test below fails on d864926 and passes after the fix.
# ---------------------------------------------------------------------------


def _full_proof_cc_demo(checked_offset_s: int = 60) -> dict[str, Any]:
    """A fresh, fully-proven at-rest positive for cc/demo."""
    return health_cache(
        at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=checked_offset_s))
    )


# -- Finding 1: admission / visibility denial beats positive proof ----------


def test_entitlement_denial_beats_positive_proof():
    # Fresh full-proof cc/demo, but current admission denies at ENTITLED stage
    # (permission). The current denial must win: UNAVAILABLE, not VERIFIED.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    facts = {
        "cc/demo": {"admitted": False, "first_failed_stage": "ENTITLED", "reason": "permission"}
    }
    snaps = snapshots_from_documents(
        health_cache_doc=_full_proof_cc_demo(), admission_facts=facts, now=NOW
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.restriction == "entitlement_denied"
    assert r.reason == "permission"


def test_permission_reason_denial_beats_positive_proof():
    # Admission reason alone (auth/payment/permission/quota) is a current denial
    # even without an explicit ENTITLED stage label.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    facts = {"cc/demo": {"admitted": False, "first_failed_stage": "", "reason": "payment_required"}}
    snaps = snapshots_from_documents(
        health_cache_doc=_full_proof_cc_demo(), admission_facts=facts, now=NOW
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE


def test_capability_gate_beats_positive_proof_is_excluded():
    # Declared capability / context gate is EXCLUDED, evaluated before positives.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    facts = {
        "cc/demo": {
            "admitted": False,
            "first_failed_stage": "CAPABILITY",
            "reason": "context_too_small",
        }
    }
    snaps = snapshots_from_documents(
        health_cache_doc=_full_proof_cc_demo(), admission_facts=facts, now=NOW
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.EXCLUDED
    assert r.restriction == "capability_excluded"
    assert r.reason == "context_too_small"
    assert r.refreshable is False


def test_visibility_unavailable_beats_positive_proof():
    # Required harness visibility is unavailable -> UNAVAILABLE even with proof.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=_full_proof_cc_demo(), visibility_unavailable=["cc/demo"], now=NOW
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.restriction == "visibility_unavailable"
    assert r.evidence_source == "harness_visibility"


def test_visibility_fact_without_proof_is_unavailable():
    # Visibility fact is consulted even when there is no at-rest evidence at all.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(visibility_unavailable=["cc/demo"], now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert "visibility_unavailable" in r.restrictions


def test_visibility_unavailable_does_not_sink_sibling_route():
    # Only the named route is sunk; a sibling with proof stays VERIFIED.
    inv = [row("omniroute/cc/demo"), row("omniroute/cc/sonnet")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            at_rest_entry("cc/sonnet", checked_at=NOW - timedelta(seconds=60)),
        ),
        visibility_unavailable=["cc/demo"],
        now=NOW,
    )
    by_id = {r.route_id: r for r in project_one(inv, conns, snaps).rows}
    assert by_id["cc/demo"].status is VerifiedStatus.UNAVAILABLE
    assert by_id["cc/sonnet"].status is VerifiedStatus.VERIFIED


# -- Finding 2: bound pool / account cooldowns beat positive proof ----------


def test_bound_pool_cooldown_beats_positive_proof():
    # A pool:p-1 cooldown bound to the route (via the inventory pool marker)
    # must sink the route even over a fresh positive.
    inv = [row("omniroute/cc/demo", subscription_pool_id="p-1")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "pool:p-1": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=300))}
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope == "pool:p-1"


def test_bound_account_cooldown_beats_positive_proof():
    # An account:acct-1 cooldown bound via the active connection's account_id.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc", account_id="acct-1")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "account:acct-1": {
                    "category": "rate_limited",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope == "account:acct-1"


def test_bound_pool_cooldown_does_not_sink_unrelated_route():
    # pool:p-1 cooldown must not sink a route bound to a different pool.
    inv = [
        row("omniroute/cc/demo", subscription_pool_id="p-1"),
        row("omniroute/cc/other", subscription_pool_id="p-2"),
    ]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            at_rest_entry("cc/other", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "pool:p-1": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=300))}
            },
        ),
        now=NOW,
    )
    by_id = {r.route_id: r for r in project_one(inv, conns, snaps).rows}
    assert by_id["cc/demo"].status is VerifiedStatus.UNAVAILABLE
    assert by_id["cc/other"].status is VerifiedStatus.VERIFIED


def test_unbound_account_cooldown_does_not_sink_route():
    # An account:acct-9 cooldown with no binding to this route is ignored.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc", account_id="acct-1")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "account:acct-9": {
                    "category": "rate_limited",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED


def test_route_bound_cooldown_via_at_rest_pool_field():
    # The at-rest entry's own ``pool`` field also binds a pool cooldown.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60), pool="p-7"),
            cooldowns={
                "pool:p-7": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=300))}
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope == "pool:p-7"


# -- Finding 3: source errors never leak secrets ----------------------------


def test_source_error_from_bad_category_redacts_api_key():
    secret = "TOPSECRET"
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    entry = at_rest_entry(
        "cc/demo",
        checked_at=NOW - timedelta(seconds=60),
        category=f"authentication api_key={secret}",
    )
    snaps = snapshots_from_documents(
        health_cache_doc={"schema_version": "1", "routes": {"cc/demo": entry}}, now=NOW
    )
    import json as _json

    blob = _json.dumps(project_one(inv, conns, snaps).to_dict())
    assert secret not in blob
    assert "[redacted]" in blob


def test_source_error_from_bad_identity_redacts_bearer_token():
    secret = "sk-abc123"
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    entry = at_rest_entry(
        "cc/demo", checked_at=NOW - timedelta(seconds=60), identity=f"Bearer {secret}"
    )
    snaps = snapshots_from_documents(
        health_cache_doc={"schema_version": "1", "routes": {"cc/demo": entry}}, now=NOW
    )
    import json as _json

    blob = _json.dumps(project_one(inv, conns, snaps).to_dict())
    assert secret not in blob
    assert "sk-abc123" not in blob


def test_extra_source_errors_are_redacted():
    # Secrets carried in extra_source_errors are scrubbed at the boundary too.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        now=NOW,
        extra_source_errors=[
            "loader failed: api_key=TOPSECRET",
            "auth header Bearer sk-abc123 rejected",
        ],
    )
    import json as _json

    blob = _json.dumps(project_one(inv, conns, snaps).to_dict())
    assert "TOPSECRET" not in blob
    assert "sk-abc123" not in blob


# -- Non-blocking: secondary restrictions preserved; worker half-open ------


def test_cooldown_preserves_secondary_identity_restriction():
    # An active cooldown wins the status, but a healthy-but-unverified identity
    # remains observable as a secondary restriction (design s2).
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    entry = at_rest_entry(
        "cc/demo",
        checked_at=NOW - timedelta(seconds=60),
        identity="not_reported",  # HTTP ok but identity never verified
    )
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            entry,
            cooldowns={
                "provider:cc": {
                    "category": "rate_limited",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert "availability_blocked" in r.restrictions
    assert "identity_not_verified" in r.restrictions


def test_expired_worker_negative_keeps_half_open_observable():
    # An expired worker negative must leave the half-open reason observable,
    # not silently vanish before the UNVERIFIED rule can read it.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    worker = {
        "cc/demo": {
            "healthy": False,
            "category": "upstream",
            "status_code": 503,
            "observed_at": iso(NOW - timedelta(seconds=400)),
            "expires_at": iso(NOW - timedelta(seconds=10)),  # expired
        }
    }
    snaps = snapshots_from_documents(worker_health_doc=worker, now=NOW)
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNVERIFIED
    assert "half_open" in r.restrictions
