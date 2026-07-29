from __future__ import annotations

from datetime import datetime, timedelta, timezone

from verdict.memory_plane import MemoryPlane
from verdict.omniroute_catalog import (
    CatalogQualificationError,
    probe_catalog,
    qualify_catalog,
    reconcile_catalog_projections,
    select_probe_models,
    store_qualification,
    summarize_probes,
)
from verdict.probes import ProbeObservation

NOW = datetime(2026, 7, 28, 5, 0, tzinfo=timezone.utc)


def _public(rows: list[object]) -> dict[str, object]:
    return {"object": "list", "data": rows}


def _row(model_id: str, **kwargs: object) -> dict[str, object]:
    return {"id": model_id, "owned_by": model_id.split("/", 1)[0], **kwargs}


def test_catalog_reconciles_duplicates_and_qualifies_explicit_profiles() -> None:
    payload = _public(
        [
            _row(
                "openrouter/code-model:free",
                capabilities={"tool_calling": True, "structured_output": True},
                context_length=1_000_000,
                max_output_tokens=8_192,
            ),
            _row("openrouter/code-model:free", type="audio", capabilities={"tool_calling": True}),
            _row(
                "reasoning/model",
                capabilities={"reasoning": True, "thinking": True},
                context_length=128_000,
            ),
        ]
    )

    report = qualify_catalog(
        payload,
        source_url="http://127.0.0.1:20128/v1/models",
        captured_at=NOW,
        now=NOW,
        expected_row_count=3,
    )

    assert report.passed is True
    assert report.snapshot is not None
    assert report.snapshot.duplicate_row_delta == 1
    assert report.snapshot.duplicate_ids[0]["classification"] == "multi-projection"
    assert report.snapshot.profile_counts["tool_use"] == 2
    assert report.snapshot.profile_counts["structured_output"] == 1
    assert report.snapshot.profile_counts["long_context"] == 1
    assert report.snapshot.profile_counts["free_tier"] == 2


def test_management_catalog_adds_provider_projection_and_version() -> None:
    payload = {
        "catalogVersion": "model-metadata-v1:static",
        "catalog": {"example": {"active": True, "models": [{"id": "example/model"}]}},
    }
    report = qualify_catalog(
        payload,
        source_url="http://127.0.0.1:20128/api/models/catalog",
        captured_at=NOW,
        now=NOW,
        expected_row_count=1,
    )
    assert report.passed is True
    assert report.snapshot is not None
    assert report.snapshot.catalog_version == "model-metadata-v1:static"
    assert report.snapshot.provider_counts == {"example": 1}


def test_schema_drift_is_unknown_and_partial_catalog_is_not_healthy() -> None:
    drift = qualify_catalog(
        {"items": []}, source_url="https://example.test/models", captured_at=NOW, now=NOW
    )
    partial = qualify_catalog(
        _public([_row("example/model")]),
        source_url="https://example.test/models",
        captured_at=NOW,
        now=NOW,
        expected_row_count=2,
    )
    assert drift.status == "unknown"
    assert partial.status == "partial"
    assert partial.passed is False
    malformed = qualify_catalog(
        _public([_row("example/model"), {"id": ""}]),
        source_url="https://example.test/models",
        captured_at=NOW,
        now=NOW,
        expected_row_count=2,
    )
    assert malformed.status == "partial"


def test_stale_snapshot_is_not_healthy() -> None:
    report = qualify_catalog(
        _public([_row("example/model")]),
        source_url="https://example.test/models",
        captured_at=NOW,
        freshness_seconds=30,
        expected_row_count=1,
        now=NOW + timedelta(seconds=30),
    )
    assert report.status == "stale"
    assert report.passed is False


def test_probe_summary_separates_ready_and_unknown_states() -> None:
    observations = [
        ProbeObservation("ready/model", "ready", "ready", NOW, latency_ms=12.5, http_status=200),
        ProbeObservation(
            "limited/model", "degraded", "failed", NOW, http_status=429, error_class="rate_limited"
        ),
        ProbeObservation(
            "missing/model", "degraded", "failed", NOW, http_status=404, error_class="http_error"
        ),
    ]
    summary = summarize_probes(observations, captured_at=NOW)
    assert summary.attempted == 3
    assert summary.ready == 1
    assert summary.non_ready == 2
    assert summary.statuses == {"failed": 2, "ready": 1}
    assert summary.error_classes == {"http_error": 1, "rate_limited": 1}
    assert all("response" not in item for item in summary.results)
    assert summary.catalog_payload_hash is None


def test_probe_selection_is_provider_diverse_and_bounded() -> None:
    payload = _public([_row("a/one:free"), _row("a/two:free"), _row("b/one:free"), _row("c/one")])
    selected = select_probe_models(payload, limit=3)
    assert selected == ("a/one:free", "b/one:free", "c/one")


def test_probe_catalog_preserves_timeout_unauthorized_quota_and_malformed() -> None:
    payload = _public(
        [_row("a/ready"), _row("b/unauthorized"), _row("c/quota"), _row("d/malformed")]
    )

    def transport(model_id: str, request: object, timeout_seconds: float) -> object:
        del request, timeout_seconds
        if model_id == "a/ready":
            return {
                "status_code": 200,
                "body": {
                    "usage": {"total_tokens": 1},
                    "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                },
            }
        if model_id == "b/unauthorized":
            return {"status_code": 401, "body": {}}
        if model_id == "c/quota":
            return {"status_code": 429, "body": {}}
        return {"status_code": 200, "body": {}}

    summary = probe_catalog(payload, transport, limit=4, captured_at=NOW)
    assert summary.attempted == 4
    assert summary.ready == 1
    assert summary.statuses == {"failed": 2, "ready": 1, "usage_unavailable": 1}
    assert summary.error_classes == {"rate_limited": 1, "unauthorized": 1}
    assert summary.catalog_payload_hash


def test_probe_catalog_rejects_unbounded_sample() -> None:
    payload = _public([_row("a/model")])
    try:
        select_probe_models(payload, limit=17)
    except CatalogQualificationError as exc:
        assert "probe sample" in str(exc)
    else:
        raise AssertionError("unbounded probe sample was accepted")


def test_qualification_is_stored_with_hash_and_freshness_provenance(tmp_path) -> None:
    report = qualify_catalog(
        _public([_row("example/model")]),
        source_url="http://127.0.0.1:20128/v1/models",
        captured_at=NOW,
        expected_row_count=1,
        now=NOW,
    )
    record = store_qualification(report, memory_path=tmp_path / "memory.db")
    assert record.namespace == "omniroute-catalog"
    assert record.provenance["payload_hash"] == report.snapshot.payload_hash
    assert record.provenance["qualification_hash"] == record.metadata["qualification_hash"]
    assert record.expires_at == report.snapshot.fresh_until.timestamp()
    with MemoryPlane(tmp_path / "memory.db") as plane:
        assert plane.history(
            "omniroute-catalog", "http://127.0.0.1:20128/v1/models", scope="shared"
        )


def test_qualification_storage_is_idempotent_for_same_snapshot(tmp_path) -> None:
    report = qualify_catalog(
        _public([_row("example/model")]),
        source_url="http://127.0.0.1:20128/v1/models",
        captured_at=NOW,
        expected_row_count=1,
        now=NOW,
    )
    first = store_qualification(report, memory_path=tmp_path / "memory.db")
    second = store_qualification(report, memory_path=tmp_path / "memory.db")
    assert first.record_id == second.record_id
    with MemoryPlane(tmp_path / "memory.db") as plane:
        assert (
            len(
                plane.history(
                    "omniroute-catalog", "http://127.0.0.1:20128/v1/models", scope="shared"
                )
            )
            == 1
        )


def test_stale_or_partial_reports_are_stored_as_non_healthy(tmp_path) -> None:
    report = qualify_catalog(
        _public([_row("example/model")]),
        source_url="https://example.test/models",
        captured_at=NOW,
        freshness_seconds=1,
        expected_row_count=2,
        now=NOW + timedelta(seconds=1),
    )
    record = store_qualification(report, memory_path=tmp_path / "memory.db")
    assert report.status == "stale"
    assert record.confidence == 0.0


def test_catalog_projections_reconcile_without_overriding_expected_count_policy() -> None:
    public_payload = _public([_row("a/model"), _row("b/model")])
    management_payload = {
        "catalogVersion": "model-metadata-v1:static",
        "catalog": {
            "a": {"active": True, "models": [{"id": "a/model"}]},
            "b": {"active": True, "models": [{"id": "b/model"}]},
        },
    }
    public = qualify_catalog(
        public_payload,
        source_url="https://example.test/v1/models",
        captured_at=NOW,
        now=NOW,
        expected_row_count=3,
    )
    management = qualify_catalog(
        management_payload,
        source_url="https://example.test/api/models/catalog",
        captured_at=NOW,
        now=NOW,
        expected_row_count=3,
    )
    reconciliation = reconcile_catalog_projections(public, management)
    assert reconciliation.status == "consistent"
    assert reconciliation.passed is True
    assert public.status == "partial"
    assert management.status == "partial"


def test_contradictory_catalog_projections_are_not_qualified() -> None:
    public = qualify_catalog(
        _public([_row("a/model")]),
        source_url="https://example.test/v1/models",
        captured_at=NOW,
        now=NOW,
        expected_row_count=1,
    )
    management = qualify_catalog(
        {
            "catalogVersion": "model-metadata-v1:static",
            "catalog": {"b": {"active": True, "models": [{"id": "b/model"}]}},
        },
        source_url="https://example.test/api/models/catalog",
        captured_at=NOW,
        now=NOW,
        expected_row_count=1,
    )
    reconciliation = reconcile_catalog_projections(public, management)
    assert reconciliation.status == "contradictory"
    assert reconciliation.passed is False
    assert "provider_counts" in reconciliation.mismatches


def test_projection_reconciliation_is_unknown_when_a_projection_has_schema_drift() -> None:
    valid = qualify_catalog(
        _public([_row("a/model")]),
        source_url="https://example.test/v1/models",
        captured_at=NOW,
        now=NOW,
        expected_row_count=1,
    )
    unknown = qualify_catalog(
        {"items": []},
        source_url="https://example.test/api/models/catalog",
        captured_at=NOW,
        now=NOW,
    )
    reconciliation = reconcile_catalog_projections(valid, unknown)
    assert reconciliation.status == "unknown"
    assert reconciliation.passed is False


def test_unknown_schema_cannot_be_stored(tmp_path) -> None:
    report = qualify_catalog(
        {"items": []}, source_url="https://example.test/models", captured_at=NOW, now=NOW
    )
    try:
        store_qualification(report, memory_path=tmp_path / "memory.db")
    except CatalogQualificationError as exc:
        assert "valid snapshot" in str(exc)
    else:
        raise AssertionError("unknown catalog was stored")


def test_committed_evidence_artifact_is_sanitized_and_matches_reconciliation() -> None:
    import json
    from pathlib import Path

    evidence = json.loads(
        Path("docs/evidence/omniroute-catalog-qualification-2026-07-28.json").read_text()
    )
    assert evidence["public"]["rows"] == 3977
    assert evidence["public"]["unique_ids"] == 3964
    assert evidence["public"]["duplicate_row_delta"] == 13
    assert evidence["liveness_sample"]["raw_responses_stored"] is False
    serialized = json.dumps(evidence).lower()
    assert "choices" not in serialized
    assert "usage" not in serialized
    assert "bearer" not in serialized


def test_refreshed_evidence_preserves_partial_baseline_and_hash_provenance() -> None:
    import json
    from pathlib import Path

    evidence = json.loads(
        Path("docs/evidence/omniroute-catalog-qualification-2026-07-29.json").read_text()
    )
    assert evidence["expected_row_policy"] == 3977
    assert evidence["public"]["rows"] == 4031
    assert evidence["management"]["rows"] == 4031
    assert evidence["public"]["target_row_delta"] == 54
    assert evidence["projection_reconciliation"]["passed"] is True
    assert evidence["liveness_sample"]["protected_work_ready"] is False
    serialized = json.dumps(evidence).lower()
    assert "bearer" not in serialized
