"""Core model metadata store — offline unit tests (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from jsonschema import Draft202012Validator

from verdict.metadata import (
    DROP_MAP_TARGET_MISSING,
    DROP_REQUIRED_UNKNOWN,
    DROP_STALE,
    DROP_UNMAPPED,
    METADATA_SCHEMA_VERSION,
    SOURCE_ARTIFICIAL_ANALYSIS,
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_MODELS_DEV_MODELS,
    SOURCE_OPENROUTER_AA,
    FieldProvenance,
    ModelMetadataError,
    ProvenancedField,
    default_mapping_path,
    file_transport,
    load_identity_map,
    load_store,
    lookup_omniroute_id,
    refresh_metadata,
)
from verdict.metadata.records import CapabilityCaps, ModelMetadataRecord
from verdict.metadata.sources import (
    MODELS_DEV_API_URL,
    HttpxJsonTransport,
    apply_openrouter_aa_scores,
    apply_soft_scores,
    fetch_openrouter_models_pages,
    parse_models_dev_api,
    parse_openrouter_aa_scores,
    parse_soft_score_table,
)
from verdict.metadata.store import MetadataSnapshot

FIXTURES = Path(__file__).resolve().parent.parent / "test_fixtures" / "metadata"
NOW = datetime(2026, 9, 18, 14, 0, tzinfo=timezone.utc)
FETCHED_AT = "2026-09-18T14:00:00Z"
SCHEMA = json.loads(
    Path(__file__)
    .resolve()
    .parent.parent.joinpath("verdict/schemas/model-metadata.v1.json")
    .read_text()
)


def test_metadata_transport_revalidates_redirect_target() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/private"})

    transport = HttpxJsonTransport(transport=httpx.MockTransport(handler))

    with pytest.raises(ModelMetadataError, match="redirect"):
        transport.get_json(MODELS_DEV_API_URL)
    assert requests == [MODELS_DEV_API_URL]


def test_metadata_user_agent_carries_the_installed_version() -> None:
    from verdict import __version__

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["user-agent"])
        return httpx.Response(200, json={})

    HttpxJsonTransport(transport=httpx.MockTransport(handler)).get_json(MODELS_DEV_API_URL)
    assert seen == [f"verdict-core/{__version__} (BOD-108 metadata)"]


def _load(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _refresh(tmp_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    transport = file_transport(
        models_dev_api=_load("models_dev_api.json"),
        models_dev_models=_load("models_dev_models.json"),
        litellm=_load("litellm_prices.json"),
        fetched_at=FETCHED_AT,
    )
    return refresh_metadata(
        transport=transport,
        mapping_path=FIXTURES / "omniroute_map.json",
        store_path=tmp_path / "model-metadata.json",
        now=NOW,
        persist=True,
        **kwargs,  # type: ignore[arg-type]
    )


class TestProvenanceContract:
    def test_field_requires_source_and_version_or_fetched_at(self) -> None:
        with pytest.raises(ModelMetadataError, match="version or fetched_at"):
            FieldProvenance(source="models.dev")

    def test_provenanced_field_round_trip(self) -> None:
        item = ProvenancedField(
            value=True,
            provenance=FieldProvenance(
                source=SOURCE_MODELS_DEV,
                fetched_at=FETCHED_AT,
                version="2024-07-18",
                raw_id="openai/gpt-4o-mini",
            ),
        )
        assert ProvenancedField.from_dict(item.to_dict()) == item


class TestP0Normalize:
    def test_models_dev_captures_tools_vision_structured_context(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        assert mini is not None
        assert mini.caps.tools is not None and mini.caps.tools.value is True
        assert mini.caps.vision is not None and mini.caps.vision.value is True
        assert mini.caps.structured is not None and mini.caps.structured.value is True
        assert mini.caps.context is not None and mini.caps.context.value == 128000
        assert mini.caps.tools.provenance.source == SOURCE_MODELS_DEV
        assert mini.caps.tools.provenance.fetched_at == FETCHED_AT

    def test_groq_vision_false_from_text_only_modalities(self, tmp_path: Path) -> None:
        groq = _refresh(tmp_path).record_by_id("groq/llama-3.3-70b-versatile")
        assert groq is not None
        assert groq.caps.vision is not None and groq.caps.vision.value is False
        assert groq.caps.tools is not None and groq.caps.tools.value is True

    def test_litellm_fills_unknown_structured_does_not_override_tools(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        groq = snapshot.record_by_id("groq/llama-3.3-70b-versatile")
        assert groq is not None
        assert groq.caps.structured is not None and groq.caps.structured.value is True
        assert groq.caps.structured.provenance.source == SOURCE_LITELLM
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        assert mini is not None
        assert mini.caps.tools is not None and mini.caps.tools.value is True
        assert mini.caps.tools.provenance.source == SOURCE_MODELS_DEV

    def test_litellm_disagreement_is_recorded_not_applied(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        conflicts = [
            item
            for item in snapshot.conflicts
            if item.id == "openai/gpt-4o-mini" and item.field == "tools"
        ]
        assert conflicts
        assert conflicts[0].primary_source == SOURCE_MODELS_DEV
        assert conflicts[0].secondary_source == SOURCE_LITELLM


class TestMappingAndDrops:
    def test_explicit_map_joins_omniroute_id(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mapping = load_identity_map(FIXTURES / "omniroute_map.json")
        found = lookup_omniroute_id(
            snapshot, "openrouter/meta-llama/llama-3.3-70b-instruct:free", identity_map=mapping
        )
        assert found.admitted_for_caps is True
        assert found.record is not None
        assert found.record.id == "meta/llama-3.3-70b-instruct"
        assert found.drop is None

    def test_unmapped_omniroute_id_is_named_drop(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mapping = load_identity_map(FIXTURES / "omniroute_map.json")
        found = lookup_omniroute_id(snapshot, "mystery/unknown-model", identity_map=mapping)
        assert found.admitted_for_caps is False
        assert found.drop is not None
        assert found.drop.reason == DROP_UNMAPPED

    def test_map_target_missing_is_named_drop(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mapping = load_identity_map(FIXTURES / "omniroute_map.json")
        found = lookup_omniroute_id(snapshot, "ghost/unmapped-target", identity_map=mapping)
        assert found.drop is not None
        assert found.drop.reason == DROP_MAP_TARGET_MISSING

    def test_required_unknown_is_named_drop(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mapping = load_identity_map(FIXTURES / "omniroute_map.json")
        found = lookup_omniroute_id(
            snapshot,
            "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            identity_map=mapping,
            required=("input_cost_per_million",),
        )
        assert found.drop is not None
        assert found.drop.reason == DROP_REQUIRED_UNKNOWN
        assert "input_cost_per_million" in found.drop.missing_fields
        assert found.record is not None

    def test_identity_match_without_map_entry(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        found = lookup_omniroute_id(snapshot, "openai/gpt-4o-mini")
        assert found.admitted_for_caps is True
        assert found.record is not None
        assert found.record.id == "openai/gpt-4o-mini"

    def test_unique_leaf_joins_gateway_prefix_to_models_json(self, tmp_path: Path) -> None:
        """agy/* inventory id joins via unique models.json leaf."""
        snapshot = _refresh(tmp_path)
        found = lookup_omniroute_id(snapshot, "agy/gemini-2.0-flash", required=("tools",))
        assert found.drop is None
        assert found.admitted_for_caps is True
        assert found.record is not None
        assert found.record.id == "google/gemini-2.0-flash"
        cited = found.provenance_for_receipt(("tools",))
        assert cited["tools"]["source"] == SOURCE_MODELS_DEV_MODELS

    def test_ambiguous_models_json_leaf_is_named_drop(self) -> None:
        """same leaf on two models.json rows → unmapped, not arbitrary pick."""
        prov = FieldProvenance(
            source=SOURCE_MODELS_DEV_MODELS, fetched_at=FETCHED_AT, version="fixture"
        )
        tools = ProvenancedField(value=True, provenance=prov)
        snapshot = MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=FETCHED_AT,
            sources={},
            records=(
                ModelMetadataRecord(id="google/shared-leaf", caps=CapabilityCaps(tools=tools)),
                ModelMetadataRecord(id="anthropic/shared-leaf", caps=CapabilityCaps(tools=tools)),
            ),
        )
        found = lookup_omniroute_id(snapshot, "agy/shared-leaf", required=("tools",))
        assert found.admitted_for_caps is False
        assert found.drop is not None
        assert found.drop.reason == DROP_UNMAPPED
        assert found.record is None
        assert "ambiguous" in (found.drop.detail or "").lower()

    def test_variant_suffix_without_models_json_row_is_named_drop(self, tmp_path: Path) -> None:
        """no silent strip of effort/variant suffixes to a base leaf."""
        snapshot = _refresh(tmp_path)
        found = lookup_omniroute_id(snapshot, "agy/gemini-2.0-flash-high", required=("tools",))
        assert found.drop is not None
        assert found.drop.reason == DROP_UNMAPPED
        assert found.record is None

    def test_exact_map_beats_unique_leaf(self, tmp_path: Path) -> None:
        from verdict.metadata.mapping import IdentityMap

        snapshot = _refresh(tmp_path)
        # Map agy leaf to meta model so exact map wins over google/* unique leaf.
        mapping = IdentityMap(
            omniroute_to_models_dev={"agy/gemini-2.0-flash": "meta/llama-3.3-70b-instruct"}
        )
        found = lookup_omniroute_id(snapshot, "agy/gemini-2.0-flash", identity_map=mapping)
        assert found.record is not None
        assert found.record.id == "meta/llama-3.3-70b-instruct"
        assert found.drop is None

    def test_stale_store_named_drop(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        found = lookup_omniroute_id(
            snapshot, "openai/gpt-4o-mini", now=NOW + timedelta(days=2), max_age=timedelta(hours=24)
        )
        assert found.drop is not None
        assert found.drop.reason == DROP_STALE


class TestInventNever:
    def test_soft_scores_absent_unless_fetched(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        assert mini is not None
        assert mini.scores.to_dict() == {}
        assert snapshot.sources[SOURCE_ARTIFICIAL_ANALYSIS].status == "skipped"

    def test_aa_fixture_stores_agentic_only_for_exact_id(self, tmp_path: Path) -> None:
        snapshot = _refresh(
            tmp_path, p1_payloads={SOURCE_ARTIFICIAL_ANALYSIS: _load("artificial_analysis.json")}
        )
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        groq = snapshot.record_by_id("groq/llama-3.3-70b-versatile")
        assert mini is not None and groq is not None
        assert mini.scores.aa_agentic is not None
        assert mini.scores.aa_agentic.value == 18.4
        assert mini.scores.aa_agentic.provenance.source == SOURCE_ARTIFICIAL_ANALYSIS
        assert groq.scores.aa_agentic is None

    def test_apply_soft_scores_does_not_fuzzy_match(self) -> None:
        from verdict.metadata.records import CapabilityCaps, ModelMetadataRecord

        records = {
            "openai/gpt-4o-mini": ModelMetadataRecord(
                id="openai/gpt-4o-mini", caps=CapabilityCaps()
            )
        }
        incoming = parse_soft_score_table(
            {"data": [{"id": "gpt-4o-mini", "elo": 1200}]},
            source="arena_elo",
            provenance=FieldProvenance(source="arena_elo", fetched_at=FETCHED_AT),
            field_map={"elo": "arena_elo"},
        )
        assert apply_soft_scores(records, incoming) == 0
        assert records["openai/gpt-4o-mini"].scores.arena_elo is None


class TestStoreAndSchema:
    def test_snapshot_validates_against_schema(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        Draft202012Validator(SCHEMA).validate(snapshot.to_dict())

    def test_round_trip_disk(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        loaded = load_store(tmp_path / "model-metadata.json")
        assert loaded.schema_version == METADATA_SCHEMA_VERSION
        assert loaded.to_dict() == snapshot.to_dict()

    def test_receipt_cites_source_per_cap(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path)
        mapping = load_identity_map(FIXTURES / "omniroute_map.json")
        found = lookup_omniroute_id(snapshot, "groq/llama-3.3-70b-versatile", identity_map=mapping)
        cited = found.provenance_for_receipt(("tools", "structured", "context"))
        assert cited["tools"]["source"] == SOURCE_MODELS_DEV
        assert cited["structured"]["source"] == SOURCE_LITELLM
        assert cited["context"]["source"] == SOURCE_MODELS_DEV
        assert "fetched_at" in cited["tools"] or "version" in cited["tools"]

    def test_shipped_mapping_loads(self) -> None:
        mapping = load_identity_map(default_mapping_path())
        assert (
            mapping.omniroute_to_models_dev["groq/llama-3.3-70b-versatile"]
            == "groq/llama-3.3-70b-versatile"
        )
        assert (
            "openrouter/meta-llama/llama-3.3-70b-instruct:free" in mapping.omniroute_to_models_dev
        )

    def test_parse_api_rejects_non_object(self) -> None:
        with pytest.raises(ModelMetadataError):
            parse_models_dev_api(
                [], FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED_AT)
            )


class TestCliMetadata:
    def test_refresh_and_lookup_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict import cli

        monkeypatch.setenv("HOME", str(tmp_path))
        store = tmp_path / "model-metadata.json"
        cli.cmd_metadata_refresh(
            store_path=store,
            mapping_path=FIXTURES / "omniroute_map.json",
            models_dev_api_file=FIXTURES / "models_dev_api.json",
            models_dev_models_file=FIXTURES / "models_dev_models.json",
            litellm_file=FIXTURES / "litellm_prices.json",
            output_json=True,
            now=NOW,
        )
        report = json.loads(capsys.readouterr().out)
        assert report["schema_version"] == "1"
        assert report["record_count"] >= 3
        assert SOURCE_MODELS_DEV in report["sources"]

        cli.cmd_metadata_lookup(
            "mystery/unknown-model",
            store_path=store,
            mapping_path=FIXTURES / "omniroute_map.json",
            required=("tools",),
            output_json=True,
        )
        lookup = json.loads(capsys.readouterr().out)
        assert lookup["drop"]["reason"] == DROP_UNMAPPED

        cli.cmd_metadata_lookup(
            "groq/llama-3.3-70b-versatile",
            store_path=store,
            mapping_path=FIXTURES / "omniroute_map.json",
            required=("tools", "vision"),
            output_json=True,
        )
        ok = json.loads(capsys.readouterr().out)
        assert ok["admitted_for_caps"] is True
        assert ok["provenance"]["tools"]["source"] == SOURCE_MODELS_DEV

    def test_help_lists_metadata(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict import cli

        monkeypatch.setattr(cli.sys, "argv", ["verdict", "--help"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert "metadata" in capsys.readouterr().out


def test_streaming_capability_round_trips_with_provenance_and_schema() -> None:
    provenance = FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED_AT)
    record = ModelMetadataRecord(
        id="p/stream", caps=CapabilityCaps(streaming=ProvenancedField(True, provenance))
    )
    restored = ModelMetadataRecord.from_dict(record.to_dict())
    assert restored.caps.streaming is not None
    assert restored.caps.streaming.value is True
    assert restored.caps.streaming.provenance.source == SOURCE_MODELS_DEV
    document = {
        "schema_version": "1",
        "refreshed_at": FETCHED_AT,
        "sources": {},
        "records": [record.to_dict()],
    }
    Draft202012Validator(SCHEMA).validate(document)


def test_models_dev_and_litellm_ingest_streaming_with_source_provenance() -> None:
    from verdict.metadata.sources import parse_litellm, parse_models_dev_models

    md_prov = FieldProvenance(source=SOURCE_MODELS_DEV_MODELS, fetched_at=FETCHED_AT)
    llm_prov = FieldProvenance(source=SOURCE_LITELLM, fetched_at=FETCHED_AT)
    md = parse_models_dev_models({"p/m": {"id": "p/m", "streaming": True}}, md_prov)["p/m"]
    llm = parse_litellm({"p/m": {"supports_streaming": False}}, llm_prov)["p/m"]
    assert md.caps.streaming is not None and md.caps.streaming.value is True
    assert md.caps.streaming.provenance.source == SOURCE_MODELS_DEV_MODELS
    assert llm.caps.streaming is not None and llm.caps.streaming.value is False
    assert llm.caps.streaming.provenance.source == SOURCE_LITELLM


class TestOpenRouterAAMirror:
    def test_include_p1_fixture_attaches_exact_id_scores(self, tmp_path: Path) -> None:
        snapshot = _refresh(
            tmp_path,
            include_p1=True,
            p1_payloads={SOURCE_OPENROUTER_AA: _load("openrouter_models_aa.json")},
        )
        status = snapshot.sources[SOURCE_OPENROUTER_AA]
        assert status.status == "ok"
        assert status.record_count is not None and status.record_count >= 3
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        gem = snapshot.record_by_id("google/gemini-2.0-flash")
        meta = snapshot.record_by_id("meta/llama-3.3-70b-instruct")
        assert mini is not None and gem is not None and meta is not None
        assert mini.scores.aa_coding is not None and mini.scores.aa_coding.value == 80.0
        assert mini.scores.aa_intelligence is not None and mini.scores.aa_intelligence.value == 10.0
        assert mini.scores.aa_coding.provenance.source == SOURCE_OPENROUTER_AA
        assert gem.scores.aa_intelligence is not None and gem.scores.aa_intelligence.value == 90.0
        assert gem.scores.aa_coding is not None and gem.scores.aa_coding.value == 20.0
        # numeric zero coding is real evidence, not missing
        assert meta.scores.aa_coding is not None and meta.scores.aa_coding.value == 0.0
        assert meta.scores.aa_intelligence is not None and meta.scores.aa_intelligence.value == 5.0

    def test_batch_suffix_does_not_fuzzy_create_new_record(self, tmp_path: Path) -> None:
        snapshot = _refresh(
            tmp_path,
            include_p1=True,
            p1_payloads={SOURCE_OPENROUTER_AA: _load("openrouter_models_aa.json")},
        )
        assert snapshot.record_by_id("openai/gpt-4o-mini:batch") is None
        assert snapshot.record_by_id("unknown/not-in-store") is None

    def test_invalid_numeric_scores_never_enter_store(self, tmp_path: Path) -> None:
        snapshot = _refresh(
            tmp_path,
            include_p1=True,
            p1_payloads={SOURCE_OPENROUTER_AA: _load("openrouter_models_aa.json")},
        )
        groq = snapshot.record_by_id("groq/llama-3.3-70b-versatile")
        assert groq is not None
        assert groq.scores.aa_coding is None
        assert groq.scores.aa_intelligence is None
        assert groq.scores.aa_agentic is None

    def test_include_p1_false_skips_openrouter(self, tmp_path: Path) -> None:
        snapshot = _refresh(tmp_path, include_p1=False)
        assert snapshot.sources[SOURCE_OPENROUTER_AA].status == "skipped"
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        assert mini is not None
        assert mini.scores.to_dict() == {}

    def test_openrouter_fill_null_does_not_override_existing_aa(self, tmp_path: Path) -> None:
        # First apply direct AA fixture, then OpenRouter mirror — existing wins.
        snapshot = _refresh(
            tmp_path,
            include_p1=True,
            p1_payloads={
                SOURCE_ARTIFICIAL_ANALYSIS: _load("artificial_analysis.json"),
                SOURCE_OPENROUTER_AA: _load("openrouter_models_aa.json"),
            },
        )
        mini = snapshot.record_by_id("openai/gpt-4o-mini")
        assert mini is not None and mini.scores.aa_agentic is not None
        assert mini.scores.aa_agentic.value == 18.4
        assert mini.scores.aa_agentic.provenance.source == SOURCE_ARTIFICIAL_ANALYSIS
        # coding was null from AA fixture path for agentic-focused fixture; OR may fill coding
        # AA fixture has coding_index 22.0 for mini — must be retained if applied first.
        # Order in refresh: fixture AA specs loop first, then OR — so coding 22.0 kept.
        assert mini.scores.aa_coding is not None
        assert mini.scores.aa_coding.value == 22.0
        assert mini.scores.aa_coding.provenance.source == SOURCE_ARTIFICIAL_ANALYSIS

    def test_openrouter_pagination_rejects_foreign_next(self) -> None:
        from verdict.metadata.sources import _normalize_openrouter_next

        with pytest.raises(ModelMetadataError, match=r"openrouter\.ai"):
            _normalize_openrouter_next(
                "https://evil.example/api/v1/models?offset=100",
                previous="https://openrouter.ai/api/v1/models?limit=100",
            )

    def test_openrouter_pagination_cycle_is_partial(self, tmp_path: Path) -> None:
        from verdict.metadata.sources import HttpxJsonTransport, fetch_openrouter_models_pages

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            # Always return the same next pointing at start → cycle after first follow
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "openai/gpt-4o-mini",
                            "benchmarks": {
                                "artificial_analysis": {
                                    "coding_index": 1.0,
                                    "intelligence_index": 1.0,
                                    "agentic_index": 1.0,
                                }
                            },
                        }
                    ],
                    "links": {
                        "next": "https://openrouter.ai/api/v1/models?output_modalities=text&limit=500&offset=0"
                    },
                    "total_count": 1,
                },
            )

        transport = HttpxJsonTransport(transport=httpx.MockTransport(handler), clock=lambda: NOW)
        rows, status = fetch_openrouter_models_pages(transport, max_pages=5)
        assert rows  # partial retained
        assert status.reason is not None and "cyclic" in status.reason
        assert len(calls) >= 1


class TestOpenRouterJoinAndPaginationInvariants:
    """Sol PR #887 R1/R2: exact-else-canonical never both; raw_id; malformed next."""

    def test_exact_id_else_canonical_never_both(self) -> None:
        rows = [
            {
                "id": "vendor/exact",
                "canonical_slug": "vendor/alternate",
                "benchmarks": {"artificial_analysis": {"coding_index": 88}},
            }
        ]
        attachments = parse_openrouter_aa_scores(rows, fetched_at=FETCHED_AT)
        records = {
            k: ModelMetadataRecord(id=k, caps=CapabilityCaps())
            for k in ("vendor/exact", "vendor/alternate")
        }
        applied = apply_openrouter_aa_scores(records, attachments)
        assert applied == 1
        assert records["vendor/exact"].scores.aa_coding is not None
        assert records["vendor/exact"].scores.aa_coding.value == 88
        assert records["vendor/alternate"].scores.aa_coding is None
        assert records["vendor/exact"].scores.aa_coding.provenance.raw_id == "vendor/exact"

    def test_canonical_fallback_keeps_openrouter_raw_id(self) -> None:
        rows = [
            {
                "id": "vendor/exact",
                "canonical_slug": "vendor/alternate",
                "benchmarks": {"artificial_analysis": {"coding_index": 88}},
            }
        ]
        attachments = parse_openrouter_aa_scores(rows, fetched_at=FETCHED_AT)
        only_canonical = {
            "vendor/alternate": ModelMetadataRecord(id="vendor/alternate", caps=CapabilityCaps())
        }
        assert apply_openrouter_aa_scores(only_canonical, attachments) == 1
        field = only_canonical["vendor/alternate"].scores.aa_coding
        assert field is not None
        assert field.value == 88
        assert field.provenance.raw_id == "vendor/exact"
        assert field.provenance.source == SOURCE_OPENROUTER_AA

    def test_unmatched_openrouter_row_attaches_nothing(self) -> None:
        rows = [
            {
                "id": "vendor/exact",
                "canonical_slug": "vendor/alternate",
                "benchmarks": {"artificial_analysis": {"coding_index": 88}},
            }
        ]
        attachments = parse_openrouter_aa_scores(rows, fetched_at=FETCHED_AT)
        empty = {"other/id": ModelMetadataRecord(id="other/id", caps=CapabilityCaps())}
        assert apply_openrouter_aa_scores(empty, attachments) == 0
        assert empty["other/id"].scores.aa_coding is None

    def test_malformed_next_with_incomplete_total_is_partial_or_error(self) -> None:
        rows = [
            {
                "id": "vendor/exact",
                "canonical_slug": "vendor/alternate",
                "benchmarks": {"artificial_analysis": {"coding_index": 88}},
            }
        ]

        class Transport:
            def get_json(self, url: str):
                from verdict.metadata.sources import FetchedDocument

                return FetchedDocument(
                    url=url,
                    data={"data": rows, "links": {"next": 42}, "total_count": 2},
                    fetched_at=FETCHED_AT,
                )

        got_rows, status = fetch_openrouter_models_pages(Transport())  # type: ignore[arg-type]
        assert len(got_rows) == 1
        assert status.status == "error" or (
            status.reason is not None and "partial" in status.reason
        )
        assert status.status != "ok" or (
            status.reason is not None and status.reason.startswith("partial:")
        )

    def test_malformed_pagination_does_not_erase_p0(self, tmp_path: Path) -> None:
        from verdict.metadata.sources import FixtureTransport

        # Refresh with fixture OR payload that would be fine; separate unit covers fetch.
        # P0 survival: include_p1 error path must leave models.dev records.
        class BoomTransport(FixtureTransport):
            def get_json(self, url: str):
                if "openrouter.ai" in url:
                    raise ModelMetadataError("forced openrouter failure")
                return super().get_json(url)

        # Use file_transport payloads for P0 only; force live OR path via include_p1
        # without OR fixture so transport is consulted — inject custom transport.
        transport = file_transport(
            models_dev_api=_load("models_dev_api.json"),
            models_dev_models=_load("models_dev_models.json"),
            litellm=_load("litellm_prices.json"),
            fetched_at=FETCHED_AT,
        )

        # Wrap: P0 URLs from fixture map; OR URL returns malformed pagination.
        class Mixed:
            def get_json(self, url: str):
                if "openrouter.ai" in url:
                    from verdict.metadata.sources import FetchedDocument

                    return FetchedDocument(
                        url=url,
                        data={"data": [{"id": "x"}], "links": {"next": 42}, "total_count": 9},
                        fetched_at=FETCHED_AT,
                    )
                return transport.get_json(url)

        snapshot = refresh_metadata(
            transport=Mixed(),  # type: ignore[arg-type]
            mapping_path=FIXTURES / "omniroute_map.json",
            store_path=tmp_path / "model-metadata.json",
            now=NOW,
            persist=True,
            include_p1=True,
        )
        assert len(snapshot.records) >= 4
        assert snapshot.record_by_id("openai/gpt-4o-mini") is not None
        or_status = snapshot.sources[SOURCE_OPENROUTER_AA]
        assert or_status.status == "error" or (
            or_status.reason is not None and "partial" in or_status.reason
        )
