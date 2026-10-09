from __future__ import annotations

from verdict.free_tier_admit import (
    REASON_CREDENTIAL_MISSING,
    REASON_NOT_FREE_TIER,
    REASON_OPAQUE_ROUTE_DISALLOWED,
    REASON_PAID_MODEL_NOT_ALLOWED,
    REASON_PRICE_UNKNOWN,
    REASON_PROVIDER_INACTIVE,
    REASON_PROVIDER_NOT_CONNECTED,
    REASON_PROVIDER_UNHEALTHY,
    CatalogIdentity,
    FreeTierModel,
    LiveAdmitError,
    OmniRouteAdmitSnapshot,
    ProviderConnection,
    admit_free_tier_active,
    expand_admit_for_worthiness,
    snapshot_from_payloads,
    why_not_model,
)
from verdict.intelligence import IntelligenceService
from verdict.models import ProviderConfig
from verdict.task_profile import SPEND_FREE_ONLY, SPEND_FREE_PREFERRED


def snap(*, catalog=(), free=(), connections=()):
    return OmniRouteAdmitSnapshot(tuple(catalog), tuple(free), tuple(connections))


def test_a_disconnected_provider_is_rejected():
    s = snap(catalog=[CatalogIdentity("p/m", "p")], free=[FreeTierModel("m", "p")])
    r = admit_free_tier_active(s)
    assert r.admitted == ()
    assert why_not_model(r, "p/m")[0]["reason"] == REASON_PROVIDER_NOT_CONNECTED


def test_b_inactive_provider_is_rejected():
    s = snap(
        catalog=[CatalogIdentity("p/m", "p")],
        free=[FreeTierModel("m", "p")],
        connections=[ProviderConnection("p", False)],
    )
    r = admit_free_tier_active(s)
    assert r.admitted == ()
    assert why_not_model(r, "p/m")[0]["reason"] == REASON_PROVIDER_INACTIVE


def test_c_free_suffix_without_free_metadata_is_not_free():
    s = snap(
        catalog=[CatalogIdentity("p/m:free", "p", True, 1.0, 1.0)],
        connections=[ProviderConnection("p", True, "active")],
    )
    r = admit_free_tier_active(s)
    assert r.admitted == ()
    assert why_not_model(r, "p/m:free")[0]["reason"] == REASON_NOT_FREE_TIER


def test_d_authoritative_free_connected_model_is_accepted():
    s = snap(
        catalog=[CatalogIdentity("p/m", "p")],
        free=[FreeTierModel("m", "p", "free")],
        connections=[ProviderConnection("p", True, "active")],
    )
    assert admit_free_tier_active(s).admitted == ("p/m",)


def test_e_paid_model_rejected_under_free_only():
    s = snap(
        catalog=[CatalogIdentity("p/free", "p"), CatalogIdentity("p/paid", "p", True, 2.0, 3.0)],
        free=[FreeTierModel("free", "p", "free")],
        connections=[ProviderConnection("p", True, "active")],
    )
    r = expand_admit_for_worthiness(
        admit_free_tier_active(s),
        s,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_ONLY,
    )
    assert r.admitted == ("p/free",)
    assert any(
        x.model_id == "p/paid" and x.reason == REASON_PAID_MODEL_NOT_ALLOWED for x in r.exclusions
    )


def test_f_known_paid_model_allowed_when_policy_permits():
    s = snap(
        catalog=[CatalogIdentity("p/free", "p"), CatalogIdentity("p/paid", "p", True, 2.0, 3.0)],
        free=[FreeTierModel("free", "p", "free")],
        connections=[ProviderConnection("p", True, "active")],
    )
    r = expand_admit_for_worthiness(
        admit_free_tier_active(s),
        s,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_PREFERRED,
    )
    assert r.admitted == ("p/free", "p/paid")


def test_unknown_price_never_competes_as_paid():
    s = snap(
        catalog=[CatalogIdentity("p/free", "p"), CatalogIdentity("p/unknown", "p")],
        free=[FreeTierModel("free", "p", "free")],
        connections=[ProviderConnection("p", True, "active")],
    )
    r = expand_admit_for_worthiness(
        admit_free_tier_active(s),
        s,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_PREFERRED,
    )
    assert "p/unknown" not in r.admitted
    assert why_not_model(r, "p/unknown")[0]["reason"] == REASON_PRICE_UNKNOWN


def test_l_opaque_route_is_rejected():
    s = snap(
        catalog=[CatalogIdentity("auto/best", "combo")],
        free=[FreeTierModel("auto/best", "combo")],
        connections=[ProviderConnection("combo", True, "active")],
    )
    r = admit_free_tier_active(s)
    assert not r.admitted
    assert any(x.reason == REASON_OPAQUE_ROUTE_DISALLOWED for x in r.exclusions)


def test_m_ranking_input_cannot_include_a_drop():
    s = snap(
        catalog=[CatalogIdentity("p/good", "p"), CatalogIdentity("p/bad:free", "p")],
        free=[FreeTierModel("good", "p")],
        connections=[ProviderConnection("p", True, "active")],
    )
    r = admit_free_tier_active(s)
    assert r.admitted == ("p/good",)
    assert "p/bad:free" not in r.admitted


def test_n_zero_eligible_has_structured_evidence():
    r = admit_free_tier_active(snap(catalog=[CatalogIdentity("p/m", "p")]))
    evidence = why_not_model(r, "p/m")
    assert r.empty_intersection and evidence and evidence[0]["model"] == "p/m"


def test_p_fallback_expansion_cannot_violate_free_only():
    s = snap(
        catalog=[CatalogIdentity("p/paid", "p", True, 1.0, 1.0)],
        connections=[ProviderConnection("p", True, "active")],
    )
    r = expand_admit_for_worthiness(
        admit_free_tier_active(s),
        s,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_ONLY,
    )
    assert r.admitted == () and r.empty_intersection


def test_catalog_parser_does_not_infer_price_or_free_from_name():
    s = snapshot_from_payloads(
        catalog={"data": [{"id": "p/x:free", "owned_by": "p"}]},
        free_tier={"perModel": []},
        providers={"connections": [{"provider": "p", "isActive": True}]},
    )
    assert s.catalog[0].price_known is False
    assert admit_free_tier_active(s).admitted == ()


def _service_for_refresh() -> IntelligenceService:
    return IntelligenceService(
        primary_model="p/m",
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="production",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        require_execution_path_authority=True,
    )


def test_k_stale_refresh_failure_retains_last_known_good(monkeypatch):
    service = _service_for_refresh()
    good = snap(
        catalog=[CatalogIdentity("p/m", "p")],
        free=[FreeTierModel("m", "p")],
        connections=[ProviderConnection("p", True, "active")],
    )
    calls = iter([good, LiveAdmitError("timeout", "boom")])

    def loader(*args, **kwargs):
        item = next(calls)
        if isinstance(item, Exception):
            raise item
        return item

    clock = iter([100.0, 200.0])
    monkeypatch.setattr("verdict.intelligence.load_omniroute_admit_snapshot", loader)
    monkeypatch.setattr("verdict.intelligence.time.monotonic", lambda: next(clock))
    first = service._load_admit_snapshot()
    second = service._load_admit_snapshot()
    assert first[0] is good and first[3] is None
    assert second[0] is good and second[3].startswith("timeout:")


def test_k_empty_refresh_cannot_replace_last_known_good(monkeypatch):
    service = _service_for_refresh()
    good = snap(
        catalog=[CatalogIdentity("p/m", "p")],
        free=[FreeTierModel("m", "p")],
        connections=[ProviderConnection("p", True, "active")],
    )
    empty = snap()
    calls = iter([good, empty])
    monkeypatch.setattr(
        "verdict.intelligence.load_omniroute_admit_snapshot", lambda *a, **k: next(calls)
    )
    clock = iter([100.0, 200.0])
    monkeypatch.setattr("verdict.intelligence.time.monotonic", lambda: next(clock))
    assert service._load_admit_snapshot()[0] is good
    retained = service._load_admit_snapshot()
    assert retained[0] is good and retained[3].startswith("invalid_empty:")


def test_paid_admission_uses_same_strict_provider_health_gate():
    for status, reason in (
        ("degraded", REASON_PROVIDER_UNHEALTHY),
        ("unauthorized", REASON_CREDENTIAL_MISSING),
    ):
        s = snap(
            catalog=[CatalogIdentity("p/paid", "p", True, 1.0, 2.0)],
            connections=[ProviderConnection("p", True, status)],
        )
        r = expand_admit_for_worthiness(
            admit_free_tier_active(s),
            s,
            task_class="ordinary",
            class_reasons=(),
            spend_policy=SPEND_FREE_PREFERRED,
        )
        assert "p/paid" not in r.admitted
        assert any(x.model_id == "p/paid" and x.reason == reason for x in r.exclusions)


def test_healthy_duplicate_provider_row_wins_for_free_and_paid():
    s = snap(
        catalog=[CatalogIdentity("p/free", "p"), CatalogIdentity("p/paid", "p", True, 1.0, 2.0)],
        free=[FreeTierModel("free", "p")],
        connections=[
            ProviderConnection("p", True, "unauthorized"),
            ProviderConnection("p", True, "healthy"),
        ],
    )
    r = expand_admit_for_worthiness(
        admit_free_tier_active(s),
        s,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_PREFERRED,
    )
    assert r.admitted == ("p/free", "p/paid")


def test_missing_provider_health_is_unknown_and_fails_closed():
    s = snap(
        catalog=[CatalogIdentity("p/free", "p"), CatalogIdentity("p/paid", "p", True, 1.0, 2.0)],
        free=[FreeTierModel("free", "p")],
        connections=[ProviderConnection("p", True, None)],
    )
    r = expand_admit_for_worthiness(
        admit_free_tier_active(s),
        s,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_PREFERRED,
    )
    assert r.admitted == ()
    assert any(x.reason == REASON_PROVIDER_UNHEALTHY for x in r.exclusions)


def test_zero_prices_are_parsed_as_known_zero_not_lost_to_truthiness():
    s = snapshot_from_payloads(
        catalog={"data": [{"id": "p/zero", "owned_by": "p", "pricing": {"input": 0, "output": 0}}]},
        free_tier={"perModel": []},
        providers={"connections": [{"provider": "p", "isActive": True, "testStatus": "active"}]},
    )
    assert s.catalog[0].price_known is True
    assert s.catalog[0].input_cost == 0 and s.catalog[0].output_cost == 0


# ---------------------------------------------------------------------------
# BOD-292: negative-only at-rest health-cache admission adapter
# ---------------------------------------------------------------------------


def test_at_rest_adapter_admits_negatives_only_not_positives(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    from verdict.admission import evidence_from_at_rest_health_cache

    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    path = tmp_path / "health-cache.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "routes": {
                    # A healthy positive MUST NOT be admitted as evidence.
                    "cc/good": {
                        "route_id": "cc/good",
                        "category": "ok",
                        "checked_at": now.isoformat(),
                        "until": (now + timedelta(minutes=30)).isoformat(),
                        "consecutive_failures": 0,
                        "chat_ok": True,
                        "tool_ok": True,
                        "healthy": True,
                    },
                    # An unexpired negative IS admitted.
                    "cc/dead": {
                        "route_id": "cc/dead",
                        "category": "upstream",
                        "checked_at": now.isoformat(),
                        "until": (now + timedelta(minutes=5)).isoformat(),
                        "consecutive_failures": 1,
                        "chat_ok": False,
                        "tool_ok": False,
                        "healthy": False,
                        "http_status": 500,
                    },
                    # An expired negative is omitted (half-open / not current).
                    "cc/old": {
                        "route_id": "cc/old",
                        "category": "timeout",
                        "checked_at": (now - timedelta(hours=1)).isoformat(),
                        "until": (now - timedelta(minutes=5)).isoformat(),
                        "consecutive_failures": 2,
                        "chat_ok": False,
                        "tool_ok": False,
                        "healthy": False,
                    },
                },
                "cooldowns": {
                    "provider:cc": {
                        "key": "provider:cc",
                        "category": "authentication",
                        "checked_at": now.isoformat(),
                        "until": (now + timedelta(hours=6)).isoformat(),
                        "provider_id": "cc",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    evidence = evidence_from_at_rest_health_cache(path, now=now)
    keys = {o.key for o in evidence.observations}
    assert "route:cc/dead" in keys  # unexpired negative admitted
    assert "route:cc/good" not in keys  # positive omitted
    assert "route:cc/old" not in keys  # expired negative omitted
    assert "provider:cc" in keys  # scoped cooldown admitted


def test_at_rest_adapter_absent_file_is_not_consulted(tmp_path):
    from datetime import datetime, timezone

    from verdict.admission import evidence_from_at_rest_health_cache

    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    evidence = evidence_from_at_rest_health_cache(tmp_path / "missing.json", now=now)
    assert evidence.consulted == ()
    assert evidence.observations == ()


def test_default_runtime_evidence_merges_at_rest_negatives(tmp_path):
    import json
    from datetime import datetime, timedelta, timezone

    from verdict.admission import default_runtime_evidence

    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    (tmp_path / "health-cache.json").write_text(
        json.dumps(
            {
                "schema_version": "1",
                "routes": {
                    "cc/dead": {
                        "route_id": "cc/dead",
                        "category": "authentication",
                        "checked_at": now.isoformat(),
                        "until": (now + timedelta(hours=6)).isoformat(),
                        "consecutive_failures": 1,
                        "chat_ok": False,
                        "tool_ok": False,
                        "healthy": False,
                        "http_status": 401,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    evidence = default_runtime_evidence(now=now, state_dir=tmp_path)
    keys = {o.key for o in evidence.observations}
    assert "route:cc/dead" in keys
