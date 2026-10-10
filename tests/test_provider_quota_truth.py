"""Recorded, redacted gateway evidence. No live requests or home writes."""

import json
from datetime import datetime, timezone
from pathlib import Path

from verdict.admission import admit
from verdict.orchestration.run import sanitize_connections
from verdict.orchestration.verified_models import EvidenceSnapshots, project_verified_models

NOW = datetime(2026, 10, 9, 3, 45, tzinfo=timezone.utc)
ROW = {"id": "cc/sonnet", "owned_by": "claude"}


def connection():
    return json.loads(Path("tests/fixtures/provider_quota_truth.json").read_text())


def projected(conn, now=NOW):
    return project_verified_models([ROW], [conn], EvidenceSnapshots(), now=now).rows[0]


def test_sanitizer_keeps_evidence_not_error_text():
    raw = connection()
    raw["lastError"] += " someone@example.com Bearer private-secret"
    safe = sanitize_connections([raw])[0]
    assert safe["rateLimitedUntil"] == raw["rateLimitedUntil"]
    assert safe["lastError"] == "rate_limited"
    assert safe["updatedAt"] == raw["updatedAt"]
    assert safe["lastTested"] == raw["lastTested"]
    assert "someone" not in json.dumps(safe)


def test_current_cooldown_wins_over_full_quota_and_blocks_refresh():
    conn = connection() | {"quota_percent": 100.0}
    row = projected(conn)
    assert row.status.value == "UNAVAILABLE"
    assert row.refreshable is False
    assert row.cooldown_until.isoformat() == "2026-10-09T04:07:32.416000+00:00"
    evidence = row.to_dict()["availability_evidence"][0]
    assert evidence["quota_percent"] == 100.0
    assert evidence["availability"] == "COOLDOWN"
    assert evidence["source"] == "omniroute:/api/providers"
    assert evidence["age_seconds"] == 60
    assert admit([ROW], [conn], None, now=NOW, require_runtime=False).ids == frozenset()


def test_unknown_quota_never_uses_policy_thresholds():
    evidence = projected(connection()).to_dict()["availability_evidence"][0]
    assert evidence["quota_percent"] is None
    assert evidence["quota_display"] == "UNKNOWN"
    assert evidence["quota_window"] is None


def test_expired_cooldown_is_historical_not_current():
    later = datetime(2026, 10, 9, 4, 10, tzinfo=timezone.utc)
    conn = connection()
    row = projected(conn, later)
    ev = row.to_dict()["availability_evidence"][0]
    assert ev["observed_cooldown_until"] is not None
    assert ev["availability"] != "COOLDOWN"
    assert ev["age_seconds"] == 1560


def test_expired_provider_evidence_does_not_block_admission():
    later = datetime(2026, 10, 9, 4, 10, tzinfo=timezone.utc)
    result = admit([ROW], [connection()], None, now=later, require_runtime=False)
    assert result.ids == frozenset({"cc/sonnet"})
    assert result.records[0].health == "unknown"


def test_scoped_sibling_cooldown_does_not_sink_usable_account():
    blocked = connection() | {"account_id": "acct-a"}
    usable = {"provider": "claude", "account_id": "acct-b", "isActive": True}
    view = project_verified_models([ROW], [blocked, usable], EvidenceSnapshots(), now=NOW)
    assert view.rows[0].status.value != "UNAVAILABLE"
    assert len(view.rows[0].to_dict()["availability_evidence"]) == 2
    result = admit([ROW], [blocked, usable], None, now=NOW, require_runtime=False)
    assert result.ids == frozenset({"cc/sonnet"})


def test_recent_429_loses_to_newer_success_and_unbounded_old_error_is_unknown():
    from datetime import timedelta

    from verdict.availability import QuotaEvidence

    conn = connection()
    conn.pop("rateLimitedUntil")
    conn["updatedAt"] = (NOW - timedelta(seconds=10)).isoformat()
    evidence = QuotaEvidence.from_connection(conn)
    assert evidence.current_429(NOW)
    assert not evidence.current_429(NOW, NOW)
    assert not evidence.current_429(NOW + timedelta(seconds=60))


def test_pool_provider_model_records_remain_distinct():
    from verdict.availability import QuotaEvidence

    for scope in ("pool", "provider", "model", "account"):
        ev = QuotaEvidence.from_connection(
            connection() | {"scope_type": scope, "scope_id": "scope-one"}
        )
        assert ev.scope == scope
        assert ev.quota_percent is None
