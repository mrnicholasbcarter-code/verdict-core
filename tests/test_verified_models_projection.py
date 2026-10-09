"""Unit tests for the pure verified-model projection (BOD-291 unit 1).

Offline only: injected ``now``, no ``~/.verdict`` access, no network, no probes.
"""

from __future__ import annotations

import hashlib
import json
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


# ---------------------------------------------------------------------------
# Re-review (cx/gpt-6-sol REQUEST_CHANGES round 2). Each test below fails on
# 4999e23 and passes after the fix.
# ---------------------------------------------------------------------------


# -- Finding 1: no secret leaks in ANY public row field ---------------------


def test_no_secret_in_any_row_field_from_every_input():
    # Generic leak test: feed one secret (in recognizable key=value / whitespace
    # / bearer shapes, lower-cased so pool/account normalisation cannot hide it)
    # through EVERY untrusted free-text input a row can surface, across the three
    # terminal branches that each expose a different field set (cooldown scope,
    # admission reason, policy exclusion). Assert the secret never appears
    # anywhere in json.dumps(view.to_dict()).
    import json as _json

    secret = "topsecretxyz"

    def _scenarios() -> list[tuple[list, list, Any]]:
        out: list[tuple[list, list, Any]] = []
        # (a) UNAVAILABLE via a bound pool cooldown whose scope name hides a
        #     secret; also leak-bait every other field.
        out.append(
            (
                [row("omniroute/cc/demo", pool_id=f"p1 token={secret}")],
                [conn("cc", api_key=secret, authorization=f"Bearer {secret}")],
                snapshots_from_documents(
                    health_cache_doc=health_cache(
                        at_rest_entry(
                            "cc/demo",
                            checked_at=NOW - timedelta(seconds=60),
                            healthy=False,
                            category=f"authentication api_key={secret}",
                            chat_ok=False,
                            tool_ok=False,
                            identity=f"Bearer {secret}",
                            until=NOW + timedelta(seconds=300),
                        ),
                        cooldowns={
                            f"pool:p1 token={secret}": {
                                "category": "authentication",
                                "until": iso(NOW + timedelta(seconds=300)),
                            }
                        },
                    ),
                    now=NOW,
                ),
            )
        )
        # (b) INVENTORY_ONLY via an UNKNOWN admission denial carrying a secret
        #     reason (no blocker, so the admission-reason branch runs).
        out.append(
            (
                [row("omniroute/cc/demo")],
                [conn("cc")],
                snapshots_from_documents(
                    admission_facts={
                        "cc/demo": {
                            "admitted": False,
                            "first_failed_stage": "UNKNOWN",
                            "reason": f"trouble api_key={secret}",
                        }
                    },
                    now=NOW,
                ),
            )
        )
        # (c) EXCLUDED via a policy exclusion carrying a secret reason.
        out.append(
            (
                [row("omniroute/cc/demo")],
                [conn("cc")],
                snapshots_from_documents(
                    policy_exclusions={"cc/demo": f"blocked token={secret}"}, now=NOW
                ),
            )
        )
        return out

    for inv, conns, snaps in _scenarios():
        blob = _json.dumps(project_one(inv, conns, snaps).to_dict())
        assert secret not in blob, blob


def test_unknown_admission_restriction_is_generic_code_not_raw_reason():
    # An unknown admission stage with a secret-bearing reason must map the
    # restriction/restrictions to a fixed code; the redacted detail is only in
    # the sanitized reason (never the raw text in a restriction field).
    secret = "TOPSECRET"
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        admission_facts={
            "cc/demo": {
                "admitted": False,
                "first_failed_stage": "UNKNOWN",
                "reason": f"trouble api_key={secret}",
            }
        },
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.INVENTORY_ONLY
    assert r.restriction == "not_admissible"
    assert r.restrictions == ("not_admissible",)
    assert secret not in (r.reason or "")
    assert "[redacted]" in (r.reason or "")


def test_cooldown_scope_with_whitespace_or_secret_becomes_invalid():
    # A pool cooldown whose scope name carries a secret/whitespace must not leak
    # it in cooldown_scope; the scope collapses to the safe placeholder.
    import json as _json

    secret = "TOPSECRET"
    inv = [row("omniroute/cc/demo", pool_id=f"p1 token={secret}")]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                f"pool:p1 token={secret}": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    view = project_one(inv, conns, snaps)
    r = only_row(view)
    assert r.status is VerifiedStatus.UNAVAILABLE
    # The scrub is applied at the display boundary (to_dict), where the scope
    # must collapse and the secret must be absent from the whole serialized view.
    d = view.to_dict()
    assert d["rows"][0]["cooldown_scope"] == (
        "pool:h-" + hashlib.sha256(f"p1 token={secret}".encode()).hexdigest()[:12]
    )
    assert secret not in _json.dumps(d)


def test_restrictions_vocabulary_is_fixed_in_output():
    # A restriction carrying external text at the row level is coerced to the
    # generic code at the to_dict boundary.
    from verdict.orchestration.verified_models import VerifiedModelRow

    r = VerifiedModelRow(
        route_id="cc/demo",
        provider="cc",
        status=VerifiedStatus.INVENTORY_ONLY,
        restriction="api_key=TOPSECRET",
        restrictions=("api_key=TOPSECRET", "identity_not_verified"),
        cooldown_scope="pool:p1 token=TOPSECRET",
    )
    d = r.to_dict()
    assert d["restriction"] == "admission_denied_unknown"
    assert d["restrictions"] == ["admission_denied_unknown", "identity_not_verified"]
    assert d["cooldown_scope"] == (
        "pool:h-" + hashlib.sha256(b"p1 token=TOPSECRET").hexdigest()[:12]
    )
    assert "TOPSECRET" not in str(d)


# -- Finding 2: bound account cooldowns scope to exact bindings -------------


def test_account_cooldown_does_not_over_apply_across_distinct_bindings():
    # Repro: two inventory routes explicitly bound to distinct accounts, two
    # active connections for those accounts, and a cooldown on acct1 only. Only
    # the acct1-bound route is sunk; the acct2-bound route stays VERIFIED.
    inv = [row("omniroute/cc/a", account_id="acct1"), row("omniroute/cc/b", account_id="acct2")]
    conns = [conn("cc", account_id="acct1"), conn("cc", account_id="acct2")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/a", checked_at=NOW - timedelta(seconds=60)),
            at_rest_entry("cc/b", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "account:acct1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    by_id = {r.route_id: r for r in project_one(inv, conns, snaps).rows}
    assert by_id["cc/a"].status is VerifiedStatus.UNAVAILABLE
    assert by_id["cc/a"].cooldown_scope == "account:acct1"
    assert by_id["cc/b"].status is VerifiedStatus.VERIFIED
    assert by_id["cc/b"].cooldown_scope is None


def test_single_active_connection_account_binding_still_blocks():
    # Unambiguous case: the route has no explicit binding and the provider has
    # exactly one active connection, so the connection account binds and an
    # account cooldown on it still sinks the route.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc", account_id="acct1")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "account:acct1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope == "account:acct1"


def test_ambiguous_multi_connection_account_cooldown_does_not_block_but_is_restricted():
    # Several active connections with distinct accounts and NO explicit route
    # binding: an account cooldown must not sink the route; the ambiguity is
    # recorded as a secondary restriction instead.
    inv = [row("omniroute/cc/demo")]
    conns = [conn("cc", account_id="acct1"), conn("cc", account_id="acct2")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "account:acct1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, conns, snaps))
    assert r.status is VerifiedStatus.VERIFIED
    assert r.cooldown_scope is None
    assert "account_binding_ambiguous" in r.restrictions


# -- Non-blocking: contradictory duplicate bindings fail closed ------------


def test_contradictory_binding_duplicates_fail_closed():
    # Two inventory rows for the same canonical id disagreeing only on their
    # pool binding are contradictory duplicates and must fail closed.
    inv = [
        row("omniroute/cc/demo", subscription_pool_id="p1"),
        row("omniroute/cc/demo", subscription_pool_id="p2"),
    ]
    conns = [conn("cc")]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=60)),
            cooldowns={
                "pool:p1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=300)),
                }
            },
        ),
        now=NOW,
    )
    view = project_one(inv, conns, snaps)
    r = only_row(view)
    assert r.status is VerifiedStatus.INVENTORY_ONLY
    assert r.restriction == "contradictory_inventory"
    assert any("contradictory" in e for e in view.source_errors)


# -- Round 3 privacy: every input path at the public JSON boundary -----------

_PRIVACY_INPUTS = {
    "inventory": (
        "id",
        "owned_by",
        "context_length",
        "max_input_tokens",
        "pricing",
        "pool_id",
        "subscription_pool_id",
        "account_id",
        "capabilities",
    ),
    "capabilities": ("tool_calling", "structured_output"),
    "connection": (
        "provider",
        "isActive",
        "testStatus",
        "authType",
        "plan_label",
        "pool_id",
        "subscription_pool_id",
        "account_id",
        "api_key",
        "authorization",
    ),
    "at_rest": (
        "route_id",
        "category",
        "checked_at",
        "until",
        "consecutive_failures",
        "chat_ok",
        "tool_ok",
        "healthy",
        "identity",
        "latency_ms",
        "http_status",
        "probe_class",
        "agentic_ok",
        "agentic_checked_at",
        "last_success_at",
        "pool",
    ),
    "cooldown": ("category", "canonical_category", "until", "checked_at", "pool_id", "account_id"),
    "ladder": ("healthy", "category", "checked_at"),
    "worker": ("healthy", "category", "observed_at", "expires_at", "status_code"),
    "receipt": ("route_id", "admitted", "reason"),
    "admission": ("admitted", "first_failed_stage", "reason"),
    "filter": ("provider", "search", "status"),
    "key": (
        "health_cache",
        "ladder_state",
        "worker_health",
        "admission_facts",
        "receipt_admitted",
        "visibility",
        "policy",
        "source_errors",
        "pool_cooldown",
        "account_cooldown",
        "route_cooldown",
        "provider_cooldown",
        "health_schema",
    ),
}
_PRIVACY_PAYLOADS = (
    "jane.doe@example.com",
    "api_key=TOPSECRET",
    "api_key=VERYSECRET",
    "sk-SUPERSECRET123456",
    "rk-SUPERSECRET123456",
    "pk-SUPERSECRET123456",
    "Bearer TOPSECRET",
    "eyJTOPSECRET.eyJVERYSECRET.signature1234",
)


@pytest.mark.parametrize(
    "group,field", [(group, field) for group, fields in _PRIVACY_INPUTS.items() for field in fields]
)
@pytest.mark.parametrize("payload", _PRIVACY_PAYLOADS)
def test_every_input_field_is_private(group, field, payload):
    inv = row("cc/demo")
    connection = conn("cc")
    entry = at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=10))
    cooldown = {"category": "authentication", "until": iso(NOW + timedelta(seconds=200))}
    ladder = {"healthy": False, "category": "upstream", "checked_at": iso(NOW)}
    worker = {
        "healthy": False,
        "category": "upstream",
        "expires_at": iso(NOW + timedelta(seconds=200)),
    }
    receipt = {"route_id": "cc/demo", "admitted": True, "reason": "ok"}
    admission = {"admitted": False, "first_failed_stage": "UNKNOWN", "reason": "no proof"}
    cache = health_cache(entry, cooldowns={"route:cc/demo": cooldown})
    docs = {
        "health_cache_doc": cache,
        "ladder_state_doc": {"health": {"cc/demo": ladder}},
        "worker_health_doc": {"cc/demo": worker},
        "admission_receipt_doc": {"candidates": [receipt]},
    }
    targets = {
        "inventory": inv,
        "capabilities": inv["capabilities"],
        "connection": connection,
        "at_rest": entry,
        "cooldown": cooldown,
        "ladder": ladder,
        "worker": worker,
        "receipt": receipt,
        "admission": admission,
    }
    query = VerifiedModelQuery()
    if group in targets:
        targets[group][field] = payload
        if group == "admission":
            docs["admission_facts"] = {"cc/demo": admission}
            docs.pop("health_cache_doc")
            docs.pop("ladder_state_doc")
            docs.pop("worker_health_doc")
    elif group == "filter":
        query = VerifiedModelQuery(**{field: payload})
        if field == "status":
            with pytest.raises(ValueError, match="invalid status filter"):
                project_one([inv], [connection], EvidenceSnapshots(), query=query)
            return  # Invalid status never produces a public envelope.
    elif field == "health_schema":
        cache["schema_version"] = payload
    elif field == "health_cache":
        cache["routes"] = {payload: entry}
    elif field == "ladder_state":
        docs["ladder_state_doc"] = {"health": {payload: ladder}, "cooldowns": {payload: cooldown}}
    elif field == "worker_health":
        docs["worker_health_doc"] = {payload: worker}
    elif field == "admission_facts":
        docs["admission_facts"] = {payload: "not an object"}
    elif field == "receipt_admitted":
        docs["admission_receipt_doc"] = {"admitted": [payload]}
    elif field == "visibility":
        docs["visibility_unavailable"] = [payload]
    elif field == "policy":
        docs["policy_exclusions"] = {"cc/demo": payload}
    elif field == "source_errors":
        docs["extra_source_errors"] = [payload]
    else:
        kind = field.removesuffix("_cooldown")
        cache["cooldowns"] = {f"{kind}:{payload}": cooldown}
        if kind in {"pool", "account"}:
            inv[f"{kind}_id"] = payload
            connection[f"{kind}_id"] = payload
        elif kind == "route":
            inv["id"] = payload
        else:
            inv["owned_by"] = payload
            connection["provider"] = payload
    # Let the tested input win, rather than hiding it behind another blocker.
    if group == "at_rest":
        cache.pop("cooldowns")
    if group != "ladder" and not (group == "key" and field == "ladder_state"):
        docs.pop("ladder_state_doc", None)
    if group != "worker" and not (group == "key" and field == "worker_health"):
        docs.pop("worker_health_doc", None)
    if group != "receipt" and not (group == "key" and field == "receipt_admitted"):
        docs.pop("admission_receipt_doc", None)
    if group in {"ladder", "worker"}:
        docs.pop("health_cache_doc", None)
    snaps = snapshots_from_documents(now=NOW, **docs)
    blob = json.dumps(project_one([inv], [connection], snaps, query=query).to_dict())
    for forbidden in ("@example.com", "TOPSECRET", "VERYSECRET", "sk-SUPER"):
        assert forbidden.lower() not in blob.lower(), (group, field, payload, blob)


@pytest.mark.parametrize(
    "kind,name,raw",
    [
        ("account", "acct1", False),
        ("account", "jane.doe@example.com", False),
        ("pool", "p1", True),
        ("pool", "x:free/path-1._", True),
        ("pool", "jane.doe@example.com", False),
        ("pool", "sk-SUPERSECRET123456", False),
        ("pool", "p1 token=TOPSECRET", False),
        ("pool", "p" * 129, False),
    ],
)
def test_scope_pseudonyms_are_stable(kind, name, raw):
    from verdict.orchestration.verified_models import VerifiedModelRow

    r = VerifiedModelRow(
        "cc/demo", "cc", VerifiedStatus.UNAVAILABLE, cooldown_scope=f"{kind}:{name}"
    )
    expected = (
        f"{kind}:{name}" if raw else (f"{kind}:h-" + hashlib.sha256(name.encode()).hexdigest()[:12])
    )
    assert r.to_dict()["cooldown_scope"] == expected
    assert r.to_dict()["cooldown_scope"] == expected


@pytest.mark.parametrize(
    "bad_id",
    [
        "cc/sk-SUPERSECRET123456",
        "cc/rk-SUPERSECRET123456",
        "cc/pk-SUPERSECRET123456",
        "cc/Bearer TOPSECRET",
        "cc/api_key=VERYSECRET",
        "cc/secret=TOPSECRET",
        "cc/password=TOPSECRET",
        "cc/eyJTOPSECRET.eyJVERYSECRET.signature1234",
        "cc/jane.doe@example.com",
    ],
)
def test_secret_route_ids_are_withheld_and_counts_reconcile(bad_id):
    good_ids = ["cc/claude-opus-4-8", "openrouter/x:free"]
    inv = [row(rid) for rid in [*good_ids, bad_id, "omniroute/" + bad_id]]
    view = project_one(inv, [conn("cc"), conn("openrouter")], EvidenceSnapshots())
    assert {r.route_id for r in view.rows} == set(good_ids)
    assert view.source_errors == ("route_id_withheld:1",)
    assert view.total_count == view.filtered_count == sum(view.counts_by_status.values()) == 2
    assert bad_id not in json.dumps(view.to_dict())


@pytest.mark.parametrize("field", ["provider", "search"])
def test_secret_filter_values_are_redacted(field):
    query = VerifiedModelQuery(**{field: "api_key=VERYSECRET"})
    view = project_one([row("cc/demo")], [conn("cc")], EvidenceSnapshots(), query=query)
    assert view.to_dict()["filters"][field] == "[redacted]"


# -- Round 3 bindings: explicit constraints plus associated usable paths ------


@pytest.mark.parametrize(
    "binding,connections,cooldowns,status,ambiguous",
    [
        ({}, [conn("cc", pool_id="p1")], ["pool:p1"], "UNAVAILABLE", None),
        ({}, [conn("cc", account_id="acct1")], ["account:acct1"], "UNAVAILABLE", None),
        (
            {"pool_id": "p1"},
            [conn("cc", pool_id="p1", account_id="acct1")],
            ["account:acct1"],
            "UNAVAILABLE",
            None,
        ),
        (
            {"subscription_pool_id": "p1"},
            [conn("cc", subscription_pool_id="p1", account_id="acct1")],
            ["account:acct1"],
            "UNAVAILABLE",
            None,
        ),
        (
            {},
            [conn("cc", pool_id="p1"), conn("cc", pool_id="p2")],
            ["pool:p1"],
            "VERIFIED",
            "pool_binding_ambiguous",
        ),
        (
            {},
            [conn("cc", pool_id="p1"), conn("cc", pool_id="p2")],
            ["pool:p1", "pool:p2"],
            "UNAVAILABLE",
            None,
        ),
        (
            {},
            [conn("cc", account_id="acct1"), conn("cc", account_id="acct2")],
            ["account:acct1"],
            "VERIFIED",
            "account_binding_ambiguous",
        ),
        (
            {},
            [conn("cc", account_id="acct1"), conn("cc", account_id="acct2")],
            ["account:acct1", "account:acct2"],
            "UNAVAILABLE",
            None,
        ),
        (
            {},
            [
                conn("cc", pool_id="p1", account_id="acct1"),
                conn("cc", pool_id="p2", account_id="acct2"),
            ],
            ["pool:p1", "account:acct2"],
            "UNAVAILABLE",
            None,
        ),
        ({"pool_id": "p1"}, [conn("cc", pool_id="p1")], ["pool:p2"], "VERIFIED", None),
        (
            {"account_id": "acct1"},
            [conn("cc", account_id="acct1")],
            ["account:acct2"],
            "VERIFIED",
            None,
        ),
        (
            {},
            [conn("cc", active=False, pool_id="p1"), conn("cc", pool_id="p2")],
            ["pool:p1"],
            "VERIFIED",
            None,
        ),
        (
            {},
            [conn("cc", active=False, account_id="acct1"), conn("cc", account_id="acct2")],
            ["account:acct1"],
            "VERIFIED",
            None,
        ),
        (
            {},
            [conn("cc", pool_id="p1"), conn("cc", active=False, pool_id="p2")],
            ["pool:p1"],
            "UNAVAILABLE",
            None,
        ),
        (
            {},
            [conn("cc", account_id="acct1"), conn("cc", active=False, account_id="acct2")],
            ["account:acct1"],
            "UNAVAILABLE",
            None,
        ),
        ({}, [conn("cc", pool_id="p2"), conn("kr", pool_id="p1")], ["pool:p1"], "VERIFIED", None),
        (
            {},
            [conn("cc", account_id="acct2"), conn("kr", account_id="acct1")],
            ["account:acct1"],
            "VERIFIED",
            None,
        ),
        (
            {"account_id": "acct1"},
            [
                conn("cc", account_id="acct1", pool_id="p1"),
                conn("cc", account_id="acct2", pool_id="p2"),
            ],
            ["pool:p1"],
            "UNAVAILABLE",
            None,
        ),
        (
            {"pool_id": "p1", "account_id": "acct1"},
            [
                conn("cc", pool_id="p1", account_id="acct2"),
                conn("cc", pool_id="p2", account_id="acct1"),
            ],
            ["account:acct2", "pool:p2"],
            "VERIFIED",
            None,
        ),
        (
            {"pool_id": "p1"},
            [
                conn("cc", pool_id="p1", account_id="acct1"),
                conn("cc", pool_id="p1", account_id="acct2"),
            ],
            ["account:acct1"],
            "VERIFIED",
            "account_binding_ambiguous",
        ),
        (
            {"pool_id": "p1"},
            [
                conn("cc", pool_id="p1", account_id="acct1"),
                conn("cc", pool_id="p1", account_id="acct2"),
            ],
            ["account:acct1", "account:acct2"],
            "UNAVAILABLE",
            None,
        ),
        (
            {"account_id": "acct1"},
            [
                conn("cc", pool_id="p1", account_id="acct1"),
                conn("cc", pool_id="p2", account_id="acct1"),
            ],
            ["pool:p1"],
            "VERIFIED",
            "pool_binding_ambiguous",
        ),
        (
            {"account_id": "acct1"},
            [
                conn("cc", pool_id="p1", account_id="acct1"),
                conn("cc", pool_id="p2", account_id="acct1"),
            ],
            ["pool:p1", "pool:p2"],
            "UNAVAILABLE",
            None,
        ),
        ({"pool_id": "p1"}, [conn("cc", pool_id="p2")], ["pool:p1"], "UNAVAILABLE", None),
        (
            {"account_id": "acct1"},
            [conn("cc", account_id="acct2")],
            ["account:acct1"],
            "UNAVAILABLE",
            None,
        ),
        (
            {"pool_id": "p1"},
            [conn("cc", pool_id="p2", account_id="acct1")],
            ["account:acct1"],
            "VERIFIED",
            None,
        ),
        (
            {"account_id": "acct1"},
            [conn("cc", account_id="acct2", pool_id="p1")],
            ["pool:p1"],
            "VERIFIED",
            None,
        ),
        ({}, [conn("cc")], ["pool:p1", "account:acct1"], "VERIFIED", None),
        (
            {},
            [conn("cc", pool_id="p1"), conn("cc")],
            ["pool:p1"],
            "VERIFIED",
            "pool_binding_ambiguous",
        ),
        (
            {"owned_by": "cc/acct1"},
            [conn("cc", account_id="acct1", pool_id="p1")],
            ["pool:p1"],
            "UNAVAILABLE",
            None,
        ),
    ],
)
def test_connection_association_binding_matrix(binding, connections, cooldowns, status, ambiguous):
    rid = "cc/acct1/demo" if "owned_by" in binding else "cc/demo"
    inv = [row(rid, **binding)]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry(rid, checked_at=NOW - timedelta(seconds=10)),
            cooldowns={
                key: {"category": "authentication", "until": iso(NOW + timedelta(seconds=200))}
                for key in cooldowns
            },
        ),
        now=NOW,
    )
    r = only_row(project_one(inv, connections, snaps))
    assert r.status.value == status
    if status == "UNAVAILABLE":
        assert r.cooldown_scope in cooldowns
        assert r.coding_ok is False
    else:
        assert r.cooldown_scope is None
        assert r.coding_ok is True
    assert {code for code in r.restrictions if code.endswith("binding_ambiguous")} == (
        {ambiguous} if ambiguous else set()
    )


@pytest.mark.parametrize(
    "binding,expected",
    [
        ({}, {"cc/a": ("VERIFIED", "pool_binding_ambiguous"), "cc/b": ("VERIFIED", None)}),
        ({"account_id": "acct2"}, {"cc/a": ("VERIFIED", None), "cc/b": ("VERIFIED", None)}),
    ],
)
def test_reviewer_edge_probe_exact(binding, expected):
    inv = [row("omniroute/cc/a", **binding), row("omniroute/cc/b", pool_id="p2")]
    connections = [
        conn("cc", pool_id="p1", account_id="acct1"),
        conn("cc", pool_id="p2", account_id="acct2"),
    ]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/a", checked_at=NOW - timedelta(seconds=10)),
            at_rest_entry("cc/b", checked_at=NOW - timedelta(seconds=10)),
            cooldowns={
                "pool:p1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=200)),
                }
            },
        ),
        now=NOW,
    )
    by_id = {r.route_id: r for r in project_one(inv, connections, snaps).rows}
    for rid, (status, ambiguity) in expected.items():
        assert by_id[rid].status.value == status
        assert by_id[rid].cooldown_scope is None
        assert {c for c in by_id[rid].restrictions if c.endswith("binding_ambiguous")} == (
            {ambiguity} if ambiguity else set()
        )


def test_reviewer_binding_probe_exact():
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=10)),
            cooldowns={
                "account:acct1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=200)),
                }
            },
        ),
        now=NOW,
    )
    view = project_one(
        [row("omniroute/cc/demo", pool_id="p1")],
        [conn("cc", account_id="acct1", pool_id="p1")],
        snaps,
    )
    d = view.to_dict()["rows"][0]
    assert d["status"] == "UNAVAILABLE"
    assert d["cooldown_scope"] == "account:h-" + hashlib.sha256(b"acct1").hexdigest()[:12]


def test_reviewer_security_probe_exact():
    email = "jane.doe@example.com"
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=10)),
            cooldowns={
                "account:" + email: {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=200)),
                }
            },
        ),
        now=NOW,
    )
    view = project_one(
        [row("omniroute/cc/demo", account_id=email)], [conn("cc", account_id=email)], snaps
    )
    assert view.to_dict()["rows"][0]["cooldown_scope"] == (
        "account:h-" + hashlib.sha256(email.encode()).hexdigest()[:12]
    )
    filtered = project_one(
        [row("omniroute/cc/demo")],
        [conn("cc")],
        snapshots_from_documents(now=NOW),
        query=VerifiedModelQuery(search="api_key=VERYSECRET"),
    )
    assert filtered.to_dict()["filters"]["search"] == "[redacted]"
    withheld = project_one(
        [row("omniroute/cc/sk-SUPERSECRET123456")], [conn("cc")], snapshots_from_documents(now=NOW)
    )
    assert withheld.to_dict()["rows"] == []
    assert withheld.source_errors == ("route_id_withheld:1",)


@pytest.mark.parametrize("field", ["pool_id", "subscription_pool_id", "at_rest_pool"])
def test_explicit_pool_sources_infer_account(field):
    inv = row("cc/demo", **({field: "p1"} if field != "at_rest_pool" else {}))
    entry = at_rest_entry(
        "cc/demo", checked_at=NOW, **({"pool": "p1"} if field == "at_rest_pool" else {})
    )
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            entry,
            cooldowns={
                "account:acct1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=200)),
                }
            },
        ),
        now=NOW,
    )
    r = only_row(project_one([inv], [conn("cc", pool_id="p1", account_id="acct1")], snaps))
    assert r.status is VerifiedStatus.UNAVAILABLE


@pytest.mark.parametrize("deadline", [NOW - timedelta(seconds=1), NOW])
def test_expired_binding_cooldowns_are_not_ambiguous(deadline):
    connections = [
        conn("cc", pool_id="p1", account_id="acct1"),
        conn("cc", pool_id="p2", account_id="acct2"),
    ]
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW),
            cooldowns={
                "pool:p1": {"until": iso(deadline)},
                "account:acct1": {"until": iso(deadline)},
            },
        ),
        now=NOW,
    )
    r = only_row(project_one([row("cc/demo")], connections, snaps))
    assert r.status is VerifiedStatus.VERIFIED
    assert not any(code.endswith("binding_ambiguous") for code in r.restrictions)


@pytest.mark.parametrize(
    "field,expected",
    [
        ("route_id", "cc/demo"),
        ("provider", "cc"),
        ("status", "VERIFIED"),
        ("coding_ok", True),
        ("last_success_at", iso(NOW)),
        ("checked_at", iso(NOW)),
        ("evidence_source", "health_cache"),
        ("fresh_until", iso(NOW + timedelta(seconds=600))),
        ("expires_at", iso(NOW + timedelta(seconds=1800))),
        ("freshness", "fresh"),
        ("capabilities", {"context_window": 200_000, "tools": True, "structured": False}),
        ("restriction", None),
        ("reason", None),
        ("restrictions", []),
        ("cooldown_until", None),
        ("cooldown_scope", None),
        ("failure_category", None),
        ("http_status", 200),
        ("latency_ms", 12.5),
        ("identity", "verified"),
        ("probe_class", "full"),
        ("agentic_ok", True),
        ("agentic_checked_at", iso(NOW)),
        ("capacity_class", "subscription"),
        ("refreshable", False),
        ("refresh_reason", None),
        ("hints", ["ladder_positive_hint", "worker_positive_hint", "receipt_admitted_hint"]),
    ],
)
def test_public_row_field_audit(field, expected):
    entry = at_rest_entry(
        "cc/demo",
        checked_at=NOW,
        latency_ms=12.5,
        http_status=200,
        probe_class="full",
        agentic_ok=True,
        agentic_checked_at=iso(NOW),
    )
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(entry),
        ladder_state_doc={"health": {"cc/demo": {"healthy": True, "checked_at": iso(NOW)}}},
        worker_health_doc={
            "cc/demo": {"healthy": True, "expires_at": iso(NOW + timedelta(seconds=300))}
        },
        admission_receipt_doc={"admitted": ["cc/demo"]},
        now=NOW,
    )
    inv = row("cc/demo")
    inv["capabilities"]["structured_output"] = False
    d = project_one([inv], [conn("cc")], snaps).to_dict()["rows"][0]
    assert set(d) == _ROW_KEYS
    assert d[field] == expected


@pytest.mark.parametrize("field", sorted(_ENVELOPE_KEYS))
def test_public_envelope_field_audit(field):
    view = project_one(
        [row("cc/demo")],
        [conn("cc")],
        EvidenceSnapshots(),
        query=VerifiedModelQuery(provider="cc", search="demo"),
    )
    expected = {
        "schema": SCHEMA,
        "generated_at": iso(NOW),
        "rows": [view.rows[0].to_dict()],
        "counts_by_status": {s.value: int(s is VerifiedStatus.UNVERIFIED) for s in VerifiedStatus},
        "filtered_counts_by_status": {
            s.value: int(s is VerifiedStatus.UNVERIFIED) for s in VerifiedStatus
        },
        "total_count": 1,
        "filtered_count": 1,
        "filters": {"status": None, "provider": "cc", "search": "demo"},
        "page": 1,
        "page_size": 50,
        "page_count": 1,
        "source_errors": [],
    }
    assert view.to_dict()[field] == expected[field]


@pytest.mark.parametrize("kind", ["pool", "account"])
def test_binding_case_insensitive_matching_preserves_pseudonym_input(kind):
    name = "Jane.Doe@example.com" if kind == "account" else "Pool-A"
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW),
            cooldowns={f"{kind}:{name}": {"until": iso(NOW + timedelta(seconds=200))}},
        ),
        now=NOW,
    )
    view = project_one([row("cc/demo")], [conn("cc", **{f"{kind}_id": name.lower()})], snaps)
    assert only_row(view).status is VerifiedStatus.UNAVAILABLE
    expected = (
        f"pool:{name}"
        if kind == "pool"
        else ("account:h-" + hashlib.sha256(name.encode()).hexdigest()[:12])
    )
    assert view.to_dict()["rows"][0]["cooldown_scope"] == expected


@pytest.mark.parametrize("scope", ["pool:p1", "account:acct1"])
@pytest.mark.parametrize("source", ["health_cache", "ladder_state"])
def test_associated_cooldowns_from_each_store(scope, source):
    doc = {"cooldowns": {scope: {"until": iso(NOW + timedelta(seconds=200))}}}
    cache = health_cache(at_rest_entry("cc/demo", checked_at=NOW))
    if source == "health_cache":
        cache.update(doc)
    snaps = snapshots_from_documents(
        health_cache_doc=cache, ladder_state_doc=doc if source == "ladder_state" else None, now=NOW
    )
    r = only_row(
        project_one([row("cc/demo")], [conn("cc", pool_id="p1", account_id="acct1")], snaps)
    )
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.evidence_source == source


@pytest.mark.parametrize(
    "status", [s for s in VerifiedStatus if s is not VerifiedStatus.INVENTORY_ONLY]
)
@pytest.mark.parametrize("kind", ["pool", "account"])
def test_partial_bindings_preserve_status_and_secondary_restriction(status, kind):
    entry = at_rest_entry("cc/demo", checked_at=NOW)
    docs = {"now": NOW}
    cd = {f"{kind}:a1": {"until": iso(NOW + timedelta(seconds=200))}}
    if status is VerifiedStatus.STALE:
        entry = at_rest_entry("cc/demo", checked_at=NOW - timedelta(seconds=900))
    elif status is VerifiedStatus.FAILED:
        entry = at_rest_entry(
            "cc/demo",
            checked_at=NOW,
            healthy=False,
            category="timeout",
            chat_ok=False,
            tool_ok=False,
            until=NOW + timedelta(seconds=200),
        )
    elif status is VerifiedStatus.EXCLUDED:
        docs["policy_exclusions"] = {"cc/demo": "policy"}
    elif status is VerifiedStatus.UNAVAILABLE:
        cd["provider:cc"] = {"until": iso(NOW + timedelta(seconds=200))}
    cache = health_cache(entry, cooldowns=cd)
    if status is VerifiedStatus.UNVERIFIED:
        cache["routes"] = {}
    docs["health_cache_doc"] = cache
    snaps = snapshots_from_documents(**docs)
    connections = [conn("cc", **{f"{kind}_id": "a1"}), conn("cc", **{f"{kind}_id": "a2"})]
    r = only_row(project_one([row("cc/demo")], connections, snaps))
    assert r.status is status
    assert f"{kind}_binding_ambiguous" in r.restrictions


@pytest.mark.parametrize("kind", ["pool", "account"])
def test_partial_bindings_survive_inventory_only(kind):
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            cooldowns={f"{kind}:a1": {"until": iso(NOW + timedelta(seconds=200))}}
        ),
        admission_facts={"cc/demo": {"admitted": False, "first_failed_stage": "UNKNOWN"}},
        now=NOW,
    )
    connections = [conn("cc", **{f"{kind}_id": "a1"}), conn("cc", **{f"{kind}_id": "a2"})]
    r = only_row(project_one([row("cc/demo")], connections, snaps))
    assert r.status is VerifiedStatus.INVENTORY_ONLY
    assert f"{kind}_binding_ambiguous" in r.restrictions


@pytest.mark.parametrize("kind", ["pool", "account"])
def test_no_active_associations_does_not_promote_or_infer_bindings(kind):
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW),
            cooldowns={f"{kind}:a1": {"until": iso(NOW + timedelta(seconds=200))}},
        ),
        now=NOW,
    )
    r = only_row(
        project_one([row("cc/demo")], [conn("cc", active=False, **{f"{kind}_id": "a1"})], snaps)
    )
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope is None
    assert r.evidence_source == "connections"
    assert not any(code.endswith("binding_ambiguous") for code in r.restrictions)


def test_exhausted_associations_consider_all_blockers_for_precedence():
    snaps = snapshots_from_documents(
        health_cache_doc=health_cache(
            at_rest_entry("cc/demo", checked_at=NOW),
            cooldowns={
                "pool:p1": {"category": "rate_limited", "until": iso(NOW + timedelta(seconds=200))},
                "account:a1": {
                    "category": "authentication",
                    "until": iso(NOW + timedelta(seconds=100)),
                },
            },
        ),
        now=NOW,
    )
    r = only_row(
        project_one(
            [row("cc/demo", pool_id="p1")], [conn("cc", pool_id="p1", account_id="a1")], snaps
        )
    )
    assert r.status is VerifiedStatus.UNAVAILABLE
    assert r.cooldown_scope == "account:a1"
    assert r.failure_category == "authentication"
