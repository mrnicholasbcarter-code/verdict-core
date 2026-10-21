"""Passive identify reports evidence without granting dispatch authority."""

import json
from datetime import datetime, timezone
from pathlib import Path

from verdict.metadata.records import (
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.omniroute_evidence import parse_omniroute_evidence

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/omniroute_evidence/snapshot.json"


def payloads():
    data = json.loads(FIXTURE.read_text())
    data["v1_models"]["data"].extend(
        [
            {"id": "alias/best", "owned_by": "alpha", "root": "best"},
            {"id": "free/weak", "owned_by": "alpha", "root": "weak", "pricing": {"prompt": 0}},
            {"id": "alpha/unknown", "owned_by": "alpha", "root": "unknown"},
            {"id": "beta/cooled", "owned_by": "beta", "root": "cooled"},
        ]
    )
    rows = data["health_matrix"]["providers"][0]["accounts"][0]["models"]
    rows.extend(
        [
            {"model": "weak", "status": "healthy", "requests": 2, "successes": 2},
            {"model": "unknown", "status": "healthy", "requests": 1, "successes": 1},
        ]
    )
    data["health_matrix"]["providers"].append(
        {
            "provider": "beta",
            "accounts": [
                {
                    "models": [
                        {"model": "cooled", "status": "healthy", "requests": 3, "successes": 3}
                    ]
                }
            ],
        }
    )
    return data


def metadata():
    records = tuple(
        ModelMetadataRecord(
            id=route,
            omniroute_ids=(route,),
            scores=SoftScores(
                aa_coding=ProvenancedField(
                    value=score,
                    provenance=FieldProvenance(source="openrouter_aa", fetched_at=NOW.isoformat()),
                )
            ),
        )
        for route, score in [("a/best", 80), ("alias/best", 80), ("free/weak", 20)]
    )
    return MetadataSnapshot(
        schema_version="1", refreshed_at=NOW.isoformat(), sources={}, records=records
    )


def test_passive_report_preserves_rating_provenance_and_uncertainty():
    from verdict.identify import identify_models

    snapshot = parse_omniroute_evidence(**payloads(), now=NOW)
    report = identify_models(snapshot, metadata=metadata())
    assert report["model_calls"] == 0
    assert report["inventory_complete"] is False
    assert report["inventory_coverage"] == "catalog_only"
    assert report["traffic_scope"] == "mixed_or_unknown"
    assert report["traffic_route_alias_count"] == 5
    assert report["independent_proof_count"] is None
    assert report["tool_proof_count"] is None
    assert report["traffic_proof_strength"] == "historical_chat_only"
    assert report["dispatch_authorized"] is False
    rows = report["available_routes"]
    assert [r["route_id"] for r in rows] == ["a/best", "alias/best", "free/weak", "alpha/unknown"]
    assert (rows[0]["basis"], rows[0]["metric"], rows[0]["source"], rows[0]["as_of"]) == (
        "score",
        "aa_coding",
        "openrouter_aa",
        NOW.isoformat(),
    )
    assert rows[-1]["basis"] == "unknown"
    assert rows[-1]["rating_unknown_reason"] == "missing_or_stale_task_rating"
    assert rows[-1]["last_request_at"] is None
    assert report["excluded_routes"] == [
        {"route_id": "a/not-entitled", "reason": "traffic_last_status_error:403"},
        {"route_id": "beta/cooled", "reason": "provider_unavailable"},
    ]
    connection = report["connections"][0]
    assert connection["usable"] is True
    assert connection["quota_state"] == "unknown"
    assert connection["quota_reason"] == "quota_unreported"
    assert connection["last_tested"] == NOW.isoformat()
    assert connection["quota_observed_at"] == NOW.isoformat()
    assert all(s["fetched_at"] == NOW.isoformat() for s in report["sources"])
    assert all(s["observed_at"] is None for s in report["sources"])
    assert all(r["tool_proof"] == "unknown" for r in rows)


def test_source_errors_and_cooldowns_do_not_become_availability():
    from verdict.identify import identify_models

    data = payloads()
    data["limits"] = None
    snapshot = parse_omniroute_evidence(**data, now=NOW)
    report = identify_models(snapshot, metadata=None)
    assert report["source_errors"] == ["limits"]
    assert report["available_routes"] == []
    assert next(s for s in report["sources"] if s["name"] == "limits")["status"] == "error"
    assert all(not c["usable"] for c in report["connections"])
    data = payloads()
    data["cooldowns"]["items"] = [{"provider": "alpha", "model": "best", "remainingMs": 10000}]
    snapshot = parse_omniroute_evidence(**data, now=NOW)
    # A1's exact cooldown mapping is used, never an identify-only provider policy.
    report = identify_models(snapshot, metadata=None)
    assert report["available_routes"][-1]["basis"] == "unknown"
    assert {
        r["route_id"] for r in report["excluded_routes"] if r["reason"] == "model_cooling_down"
    } == {"a/best", "alias/best"}
    for route in snapshot.cooled_models:
        assert route not in {r["route_id"] for r in report["available_routes"]}


def test_inventory_errors_and_unobserved_routes_are_accounted_for():
    from verdict.identify import identify_models

    data = payloads()
    for status in (429, 401):
        data["v1_models"]["data"].append(
            {"id": f"alpha/error-{status}", "owned_by": "alpha", "root": f"error-{status}"}
        )
        data["health_matrix"]["providers"][0]["accounts"][0]["models"].append(
            {
                "model": f"error-{status}",
                "status": "error",
                "requests": 8,
                "successes": 2,
                "lastStatus": status,
            }
        )
    data["v1_models"]["data"].append({"id": "alpha/unobserved", "owned_by": "alpha"})
    snapshot = parse_omniroute_evidence(**data, now=NOW)
    report = identify_models(snapshot, metadata=metadata())
    excluded = {r["route_id"]: r["reason"] for r in report["excluded_routes"]}
    assert excluded["alpha/error-429"] == "traffic_last_status_error:429"
    assert excluded["alpha/error-401"] == "traffic_last_status_error:401"
    assert excluded["alpha/unobserved"] == "no_traffic_evidence"
    available = {r["route_id"] for r in report["available_routes"]}
    assert available == {"a/best", "alias/best", "free/weak", "alpha/unknown"}
    assert set(excluded) | available == {r["id"] for r in data["v1_models"]["data"]}
    assert report["inventory_complete"] is False
    assert report["inventory_coverage"] == "catalog_only"


def test_excluded_output_is_capped_with_exact_counts_and_traffic_first():
    from verdict.identify import identify_models

    data = payloads()
    data["v1_models"]["data"].extend(
        {"id": f"a/unobserved-{i:04}", "owned_by": "alpha"} for i in range(200)
    )
    snapshot = parse_omniroute_evidence(**data, now=NOW)
    report = identify_models(snapshot, metadata=None)
    assert len(report["excluded_routes"]) == 100
    assert report["excluded_routes_total"] == 202
    assert report["excluded_routes_truncated"] is True
    assert report["excluded_routes_limit"] == 100
    assert report["excluded_routes_order"] == "traffic_first_then_route_id"
    assert report["excluded_routes_by_reason"] == {
        "traffic_last_status_error:403": 1,
        "provider_unavailable": 1,
        "no_traffic_evidence": 200,
    }
    assert [r["route_id"] for r in report["excluded_routes"][:2]] == [
        "a/not-entitled",
        "beta/cooled",
    ]
    complete = identify_models(snapshot, metadata=None, all_excluded=True)
    assert len(complete["excluded_routes"]) == 202
    assert complete["excluded_routes_truncated"] is False
    assert complete["excluded_routes_by_reason"] == report["excluded_routes_by_reason"]


def test_missing_catalog_does_not_claim_catalog_coverage():
    from verdict.identify import identify_models

    data = payloads()
    data["v1_models"] = None
    report = identify_models(parse_omniroute_evidence(**data, now=NOW), metadata=None)
    assert report["inventory_coverage"] == "unknown"
    assert report["inventory_complete"] is False
