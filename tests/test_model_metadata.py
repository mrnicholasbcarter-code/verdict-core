"""BOD-108 Core model metadata store — offline unit tests (no network)."""

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
    apply_soft_scores,
    parse_models_dev_api,
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
        """BOD-121: agy/* inventory id joins via unique models.json leaf."""
        snapshot = _refresh(tmp_path)
        found = lookup_omniroute_id(snapshot, "agy/gemini-2.0-flash", required=("tools",))
        assert found.drop is None
        assert found.admitted_for_caps is True
        assert found.record is not None
        assert found.record.id == "google/gemini-2.0-flash"
        cited = found.provenance_for_receipt(("tools",))
        assert cited["tools"]["source"] == SOURCE_MODELS_DEV_MODELS

    def test_ambiguous_models_json_leaf_is_named_drop(self) -> None:
        """BOD-121: same leaf on two models.json rows → unmapped, not arbitrary pick."""
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
        """BOD-121: no silent strip of effort/variant suffixes to a base leaf."""
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
