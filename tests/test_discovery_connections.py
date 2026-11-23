from __future__ import annotations

import json

from verdict.discovery import connections


def test_discovery_reports_actual_connections_not_catalog(monkeypatch):
    monkeypatch.delenv("VERDICT_CONNECTIONS_SNAPSHOT", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "fixture-management")
    monkeypatch.setattr(connections, "check_drift", lambda: {"status": "ok"})
    from verdict import omniroute_evidence

    def read(url, name, path, key, timeout, **bounds):
        assert (url, name, path, key) == (
            "http://127.0.0.1:20128",
            "providers",
            "/api/providers",
            "fixture-management",
        )
        return {
            "connections": [
                {"id": "one", "provider": "claude", "authType": "oauth", "isActive": True},
                {"id": "two", "provider": "claude", "authType": "oauth", "isActive": False},
                {
                    "id": "three",
                    "provider": "uncurated",
                    "authType": "oauth",
                    "apiKey": "fixture-secret",
                },
            ]
        }

    monkeypatch.setattr(omniroute_evidence, "_request_json_bounded", read)
    result = connections.run_connections_action()
    assert result["status"] == "ok"
    rows = result["connections"]
    assert [row["capacity"] for row in rows] == ["subscription", "subscription", "unknown"]
    assert len({row["connection_id"] for row in rows}) == 3
    assert rows[0]["overage"] == "hard-stop"
    assert rows[0]["deciding_fact"] == "billing_snapshot_subscription:hard-stop"
    assert rows[2]["source"] is None
    encoded = json.dumps(result)
    assert "fixture-secret" not in encoded
    assert rows[0]["connection_id"] != "one"


def test_free_summary_preserves_explicit_false_and_discontinued_metadata():
    from verdict.free_tier_admit import parse_free_tier_models

    one_time, discontinued = parse_free_tier_models(
        {
            "perModel": [
                {
                    "modelId": "trial",
                    "provider": "x",
                    "freeType": "one-time-initial",
                    "hardStopGuaranteed": False,
                },
                {"modelId": "retired", "provider": "x", "freeType": "discontinued"},
            ]
        }
    )
    assert one_time.free_type == "one-time-initial"
    assert one_time.hard_stop_guaranteed is False
    assert discontinued.hard_stop_guaranteed is None


def test_cached_free_summary_applies_only_to_matching_fresh_gateway(tmp_path):
    from datetime import datetime, timedelta, timezone

    from verdict.free_tier_admit import annotate_cached_free_tier

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    origin = "http://127.0.0.1:20128"
    path = tmp_path / "admit-last-good.json"
    path.write_text(
        json.dumps(
            {
                origin: {
                    "free_tier": {
                        "fetched_at": now.isoformat(),
                        "payload": {
                            "perModel": [
                                {
                                    "modelId": "trial",
                                    "provider": "x",
                                    "freeType": "one-time-initial",
                                    "hardStopGuaranteed": True,
                                },
                                {"modelId": "retired", "provider": "x", "freeType": "discontinued"},
                            ]
                        },
                    }
                }
            }
        )
    )
    rows = [{"id": "x/trial"}, {"id": "y/trial"}, {"id": "x/retired"}]
    report = annotate_cached_free_tier(rows, origin=origin, now=now, path=path)
    assert report[0]["gateway_free_tier"] is True
    assert report[0]["free_type"] == "one-time-initial"
    assert report[0]["hard_stop_guaranteed"] is True
    assert report[1] == rows[1]
    assert report[2]["gateway_free_tier"] is False
    assert (
        annotate_cached_free_tier(rows, origin=origin, now=now + timedelta(hours=1), path=path)
        == rows
    )
    assert annotate_cached_free_tier(rows, origin="http://other", now=now, path=path) == rows


def test_discontinued_summary_row_does_not_make_route_free():
    from verdict.capacity import CapacityClass, capacity_of

    assert (
        capacity_of(
            {"provider": "x"},
            {"gateway_free_tier": True, "free_type": "discontinued"},
            route_id="x/model",
            openrouter_free_set=None,
        )[0]
        is CapacityClass.UNKNOWN
    )


def test_model_list_price_does_not_override_gateway_free_fact():
    from verdict.capacity import CapacityClass, capacity_of

    for source in ("synced", "hardcoded", "unknown"):
        row = {"pricing": {"input": 3}, "price_source": source, "gateway_free_tier": True}
        assert (
            capacity_of({"provider": "x"}, row, route_id="x/model", openrouter_free_set=None)[0]
            is CapacityClass.FREE
        )
    row = {"pricing": {"input": 3}, "price_source": "provider", "gateway_free_tier": True}
    assert (
        capacity_of({"provider": "x"}, row, route_id="x/model", openrouter_free_set=None)[0]
        is CapacityClass.METERED
    )


def test_funnel_separates_one_time_from_recurring_supply():
    from datetime import datetime, timezone

    from verdict.discovery_funnel import build_funnel

    report = {
        "task_pools": {
            "implementation": {
                "eligible": [
                    {
                        "route_id": "x/trial",
                        "capacity": "free",
                        "capacity_evidence": "gateway_free_tier:one-time-initial:false",
                    },
                    {
                        "route_id": "x/recurring",
                        "capacity": "free",
                        "capacity_evidence": "gateway_free_tier:recurring-daily:true",
                    },
                    {
                        "route_id": "x/unknown",
                        "capacity": "free",
                        "capacity_evidence": "all_zero_pricing",
                    },
                ]
            }
        }
    }
    stages = build_funnel(
        identify_report=report,
        health_cache=None,
        admission=None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )["stages"]
    assert stages["free_by_fact"]["count"] == 3
    assert stages["free_supply_by_type"] == {"one_time_initial": 1, "recurring": 1, "unknown": 1}


def test_discovery_does_not_pass_malformed_inventory_as_empty(monkeypatch):
    monkeypatch.delenv("VERDICT_CONNECTIONS_SNAPSHOT", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setattr(connections, "check_drift", lambda: {"status": "ok"})
    from verdict import omniroute_evidence

    monkeypatch.setattr(omniroute_evidence, "_request_json_bounded", lambda *a, **kw: {})
    result = connections.run_connections_action()
    assert result["status"] == "unavailable"
    assert result["connections"] == []
