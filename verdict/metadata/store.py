"""On-disk Core metadata store and refresh job (BOD-108)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.metadata.mapping import IdentityMap, load_identity_map
from verdict.metadata.records import (
    DROP_MAP_TARGET_MISSING,
    DROP_REQUIRED_UNKNOWN,
    DROP_STALE,
    DROP_UNMAPPED,
    METADATA_SCHEMA_VERSION,
    SOURCE_ARENA_ELO,
    SOURCE_ARTIFICIAL_ANALYSIS,
    SOURCE_BFCL,
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_OPEN_LLM,
    FieldConflict,
    FieldProvenance,
    MetadataDrop,
    MetadataLookup,
    ModelMetadataError,
    ModelMetadataRecord,
    SourceStatus,
    _iso,
    resolve_required_name,
)
from verdict.metadata.sources import (
    AA_FIELD_MAP,
    ARENA_FIELD_MAP,
    BFCL_FIELD_MAP,
    LITELLM_URL,
    MODELS_DEV_API_URL,
    MODELS_DEV_MODELS_URL,
    OPEN_LLM_FIELD_MAP,
    FetchedDocument,
    FixtureTransport,
    HttpxJsonTransport,
    JsonTransport,
    apply_soft_scores,
    merge_litellm_secondary,
    parse_litellm,
    parse_models_dev_api,
    parse_models_dev_models,
    parse_soft_score_table,
    skipped_p1,
)


def default_store_path() -> Path:
    return Path.home() / ".verdict" / "model-metadata.json"


@dataclass(frozen=True)
class MetadataSnapshot:
    """Persisted store: records, source statuses, mapping summary, named drops."""

    schema_version: str
    refreshed_at: str
    sources: dict[str, SourceStatus]
    records: tuple[ModelMetadataRecord, ...]
    mapping: dict[str, Any] = field(default_factory=dict)
    drops: tuple[MetadataDrop, ...] = ()
    conflicts: tuple[FieldConflict, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != METADATA_SCHEMA_VERSION:
            raise ModelMetadataError("schema_version must be '1'")
        by_id = [item.id for item in self.records]
        if len(by_id) != len(set(by_id)):
            raise ModelMetadataError("snapshot records must have unique ids")

    def record_by_id(self, models_dev_id: str) -> ModelMetadataRecord | None:
        for item in self.records:
            if item.id == models_dev_id:
                return item
        return None

    def index_omniroute(self) -> dict[str, ModelMetadataRecord]:
        index: dict[str, ModelMetadataRecord] = {}
        for item in self.records:
            index[item.id] = item
            for omni_id in item.omniroute_ids:
                index[omni_id] = item
        return index

    def is_stale(self, now: datetime, max_age: timedelta) -> bool:
        refreshed = datetime.fromisoformat(self.refreshed_at.replace("Z", "+00:00"))
        return now - refreshed > max_age

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "refreshed_at": self.refreshed_at,
            "sources": {name: status.to_dict() for name, status in sorted(self.sources.items())},
            "records": [item.to_dict() for item in self.records],
        }
        if self.mapping:
            payload["mapping"] = dict(self.mapping)
        if self.drops:
            payload["drops"] = [item.to_dict() for item in self.drops]
        if self.conflicts:
            payload["conflicts"] = [item.to_dict() for item in self.conflicts]
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MetadataSnapshot:
        if not isinstance(value, Mapping):
            raise ModelMetadataError("snapshot must be an object")
        sources_raw = value.get("sources") or {}
        if not isinstance(sources_raw, Mapping):
            raise ModelMetadataError("sources must be an object")
        records_raw = value.get("records") or []
        if not isinstance(records_raw, list):
            raise ModelMetadataError("records must be an array")
        drops_raw = value.get("drops") or []
        conflicts_raw = value.get("conflicts") or []
        mapping_raw = value.get("mapping") or {}
        if not isinstance(mapping_raw, Mapping):
            raise ModelMetadataError("mapping must be an object")
        mapping = {str(key): mapping_raw[key] for key in mapping_raw}
        sources = {
            str(name): SourceStatus.from_dict(status) if isinstance(status, Mapping) else status
            for name, status in sources_raw.items()
        }
        return cls(
            schema_version=str(value.get("schema_version", "")),
            refreshed_at=str(value.get("refreshed_at", "")),
            sources=sources,
            records=tuple(ModelMetadataRecord.from_dict(item) for item in records_raw),
            mapping=dict(mapping),
            drops=tuple(MetadataDrop.from_dict(item) for item in drops_raw),
            conflicts=tuple(
                FieldConflict(
                    id=str(item["id"]),
                    field=str(item["field"]),
                    primary_source=str(item["primary_source"]),
                    secondary_source=str(item["secondary_source"]),
                    detail=item.get("detail"),
                )
                for item in conflicts_raw
                if isinstance(item, Mapping)
            ),
        )


def load_store(path: Path | str | None = None) -> MetadataSnapshot:
    resolved = Path(path) if path is not None else default_store_path()
    document = json.loads(resolved.read_text(encoding="utf-8"))
    return MetadataSnapshot.from_dict(document)


def save_store(snapshot: MetadataSnapshot, path: Path | str | None = None) -> Path:
    resolved = Path(path) if path is not None else default_store_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        json.dumps(snapshot.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return resolved


def lookup_omniroute_id(
    snapshot: MetadataSnapshot,
    omniroute_id: str,
    *,
    required: Sequence[str] = (),
    identity_map: IdentityMap | None = None,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> MetadataLookup:
    """Resolve an OmniRoute inventory id against Core's store.

    Unmapped and required-unknown are named drops for later BOD-100. This
    function does not admit work and does not consult OmniRoute metadata.
    """
    if not isinstance(omniroute_id, str) or not omniroute_id.strip():
        raise ModelMetadataError("omniroute_id must be non-empty")
    key = omniroute_id.strip()
    if max_age is not None:
        clock = now or datetime.now(timezone.utc)
        if snapshot.is_stale(clock, max_age):
            return MetadataLookup(
                omniroute_id=key,
                record=None,
                drop=MetadataDrop(
                    omniroute_id=key,
                    reason=DROP_STALE,
                    detail=f"store refreshed_at {snapshot.refreshed_at} exceeds max_age",
                ),
            )

    index = snapshot.index_omniroute()
    models_dev_id = identity_map.models_dev_id_for(key) if identity_map is not None else None
    if models_dev_id is None and key in index:
        models_dev_id = index[key].id
    if models_dev_id is None:
        return MetadataLookup(
            omniroute_id=key,
            record=None,
            drop=MetadataDrop(
                omniroute_id=key,
                reason=DROP_UNMAPPED,
                detail="no explicit OmniRoute→models.dev map and no identity match",
            ),
        )
    record = snapshot.record_by_id(models_dev_id)
    if record is None:
        return MetadataLookup(
            omniroute_id=key,
            record=None,
            drop=MetadataDrop(
                omniroute_id=key,
                reason=DROP_MAP_TARGET_MISSING,
                detail=f"mapped to {models_dev_id} which is absent from the Core store",
            ),
        )
    missing = []
    for name in required:
        field_name = resolve_required_name(name)
        if record.caps.field(field_name) is None:
            missing.append(field_name)
    if missing:
        return MetadataLookup(
            omniroute_id=key,
            record=record,
            drop=MetadataDrop(
                omniroute_id=key,
                reason=DROP_REQUIRED_UNKNOWN,
                detail="required capability field was not fetched",
                missing_fields=tuple(missing),
            ),
        )
    return MetadataLookup(omniroute_id=key, record=record, drop=None)


def _fetch(transport: JsonTransport, url: str) -> tuple[FetchedDocument | None, SourceStatus]:
    try:
        document = transport.get_json(url)
    except Exception as exc:
        return None, SourceStatus(
            source=url, status="error", url=url, reason=f"{type(exc).__name__}: {exc}"
        )
    return document, SourceStatus(
        source=url,
        status="ok",
        url=document.url,
        fetched_at=document.fetched_at,
        version=document.version,
    )


def refresh_metadata(
    *,
    transport: JsonTransport | None = None,
    identity_map: IdentityMap | None = None,
    mapping_path: Path | str | None = None,
    store_path: Path | str | None = None,
    models_dev_api_url: str = MODELS_DEV_API_URL,
    models_dev_models_url: str = MODELS_DEV_MODELS_URL,
    litellm_url: str = LITELLM_URL,
    p1_payloads: Mapping[str, Any] | None = None,
    include_p1: bool = False,
    now: datetime | None = None,
    persist: bool = True,
) -> MetadataSnapshot:
    """Fetch P0 sources, normalize, join the explicit map, and persist the store."""
    clock = now or datetime.now(timezone.utc)
    fetched_at = _iso(clock)
    transport = transport or HttpxJsonTransport(clock=lambda: clock)
    identity_map = identity_map or load_identity_map(mapping_path)
    sources: dict[str, SourceStatus] = {}
    records: dict[str, ModelMetadataRecord] = {}

    models_doc, models_status = _fetch(transport, models_dev_models_url)
    if models_doc is not None:
        parsed = parse_models_dev_models(
            models_doc.data,
            provenance=FieldProvenance(
                source=SOURCE_MODELS_DEV,
                fetched_at=models_doc.fetched_at,
                version=models_doc.version,
            ),
        )
        records.update(parsed)
        sources[SOURCE_MODELS_DEV + ".models"] = SourceStatus(
            source=SOURCE_MODELS_DEV,
            status="ok",
            url=models_doc.url,
            fetched_at=models_doc.fetched_at,
            version=models_doc.version,
            record_count=len(parsed),
        )
    else:
        sources[SOURCE_MODELS_DEV + ".models"] = SourceStatus(
            source=SOURCE_MODELS_DEV,
            status=models_status.status,
            url=models_dev_models_url,
            reason=models_status.reason,
        )

    api_doc, api_status = _fetch(transport, models_dev_api_url)
    if api_doc is not None:
        parsed_api = parse_models_dev_api(
            api_doc.data,
            provenance=FieldProvenance(
                source=SOURCE_MODELS_DEV, fetched_at=api_doc.fetched_at, version=api_doc.version
            ),
        )
        records.update(parsed_api)
        sources[SOURCE_MODELS_DEV] = SourceStatus(
            source=SOURCE_MODELS_DEV,
            status="ok",
            url=api_doc.url,
            fetched_at=api_doc.fetched_at,
            version=api_doc.version,
            record_count=len(parsed_api),
        )
    else:
        sources[SOURCE_MODELS_DEV] = SourceStatus(
            source=SOURCE_MODELS_DEV,
            status=api_status.status,
            url=models_dev_api_url,
            reason=api_status.reason,
        )

    if (
        SOURCE_MODELS_DEV not in sources or sources[SOURCE_MODELS_DEV].status != "ok"
    ) and not records:
        raise ModelMetadataError("models.dev primary fetch failed and no records were produced")

    litellm_doc, litellm_status = _fetch(transport, litellm_url)
    conflicts: list[FieldConflict] = []
    if litellm_doc is not None:
        litellm_records = parse_litellm(
            litellm_doc.data,
            provenance=FieldProvenance(
                source=SOURCE_LITELLM,
                fetched_at=litellm_doc.fetched_at,
                version=litellm_doc.version,
            ),
        )
        raw_conflicts = merge_litellm_secondary(records, litellm_records)
        conflicts = [
            FieldConflict(
                id=model_id,
                field=field_name,
                primary_source=SOURCE_MODELS_DEV,
                secondary_source=SOURCE_LITELLM,
                detail="models.dev value kept; LiteLLM differed",
            )
            for model_id, field_name in raw_conflicts
        ]
        sources[SOURCE_LITELLM] = SourceStatus(
            source=SOURCE_LITELLM,
            status="ok",
            url=litellm_doc.url,
            fetched_at=litellm_doc.fetched_at,
            version=litellm_doc.version,
            record_count=len(litellm_records),
        )
    else:
        sources[SOURCE_LITELLM] = SourceStatus(
            source=SOURCE_LITELLM,
            status=litellm_status.status,
            url=litellm_url,
            reason=litellm_status.reason,
        )

    p1_payloads = dict(p1_payloads or {})
    _ = include_p1  # live P1 fetchers are stubs; fixtures still apply when provided
    p1_specs = (
        (SOURCE_ARTIFICIAL_ANALYSIS, AA_FIELD_MAP),
        (SOURCE_ARENA_ELO, ARENA_FIELD_MAP),
        (SOURCE_OPEN_LLM, OPEN_LLM_FIELD_MAP),
        (SOURCE_BFCL, BFCL_FIELD_MAP),
    )
    for source, field_map in p1_specs:
        payload = p1_payloads.get(source)
        if payload is None:
            sources[source] = skipped_p1(source)
            continue
        parsed_scores = parse_soft_score_table(
            payload,
            source=source,
            provenance=FieldProvenance(source=source, fetched_at=fetched_at, version="fixture"),
            field_map=field_map,
        )
        applied = apply_soft_scores(records, parsed_scores)
        sources[source] = SourceStatus(
            source=source,
            status="ok",
            fetched_at=fetched_at,
            version="fixture",
            record_count=applied,
            reason="stored only for ids that exactly match a models.dev record",
        )

    map_drops: list[MetadataDrop] = []
    for omni_id, models_dev_id in identity_map.omniroute_to_models_dev.items():
        target = records.get(models_dev_id)
        if target is None:
            map_drops.append(
                MetadataDrop(
                    omniroute_id=omni_id,
                    reason=DROP_MAP_TARGET_MISSING,
                    detail=f"map target {models_dev_id} was not present in fetched models.dev data",
                )
            )
            continue
        records[models_dev_id] = target.with_omniroute_ids((omni_id,))

    ordered = tuple(sorted(records.values(), key=lambda item: item.id))
    snapshot = MetadataSnapshot(
        schema_version=METADATA_SCHEMA_VERSION,
        refreshed_at=fetched_at,
        sources=sources,
        records=ordered,
        mapping=identity_map.to_summary(),
        drops=tuple(map_drops),
        conflicts=tuple(conflicts),
    )
    if persist:
        save_store(snapshot, store_path)
    return snapshot


def file_transport(
    *,
    models_dev_api: Any | None = None,
    models_dev_models: Any | None = None,
    litellm: Any | None = None,
    fetched_at: str,
    models_dev_api_url: str = MODELS_DEV_API_URL,
    models_dev_models_url: str = MODELS_DEV_MODELS_URL,
    litellm_url: str = LITELLM_URL,
) -> FixtureTransport:
    payloads: dict[str, Any] = {}
    if models_dev_api is not None:
        payloads[models_dev_api_url] = models_dev_api
    if models_dev_models is not None:
        payloads[models_dev_models_url] = models_dev_models
    if litellm is not None:
        payloads[litellm_url] = litellm
    return FixtureTransport(payloads=payloads, fetched_at=fetched_at, version="fixture")
