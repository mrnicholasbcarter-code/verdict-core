"""Fetch and normalize free public model metadata sources (BOD-108).

P0: models.dev (primary) and LiteLLM (secondary cross-check).
P1: stubbed unless a fixture payload is supplied — never invent scores.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from verdict.metadata.records import (
    SOURCE_ARENA_ELO,
    SOURCE_ARTIFICIAL_ANALYSIS,
    SOURCE_BFCL,
    SOURCE_OPEN_LLM,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataError,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
    SourceStatus,
    _iso,
)
from verdict.security import validate_upstream_url

MODELS_DEV_API_URL = "https://models.dev/api.json"
MODELS_DEV_MODELS_URL = "https://models.dev/models.json"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)
ARTIFICIAL_ANALYSIS_URL = "https://artificialanalysis.ai/api/v2/language/models/free"

ALLOWED_METADATA_HOSTS = frozenset(
    {
        "models.dev",
        "raw.githubusercontent.com",
        "github.com",
        "huggingface.co",
        "artificialanalysis.ai",
    }
)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_BYTES = 32 * 1024 * 1024

P1_SKIP_REASONS = {
    SOURCE_ARTIFICIAL_ANALYSIS: (
        "Artificial Analysis free API requires an API key (~100 req/day) and is not a "
        "core dependency; supply a fixture payload to store scores."
    ),
    SOURCE_ARENA_ELO: (
        "Arena Elo durable path is Hugging Face dataset lmarena-ai/leaderboard-dataset; "
        "huggingface_hub is not a core dependency. Supply a fixture to store Elo."
    ),
    SOURCE_OPEN_LLM: (
        "Open LLM Leaderboard is Hugging Face dataset open-llm-leaderboard/contents; "
        "huggingface_hub is not a core dependency. Supply a fixture to store scores."
    ),
    SOURCE_BFCL: (
        "BFCL scores are Hugging Face CSVs (gorilla-llm/Berkeley-Function-Calling-Leaderboard) "
        "with no stable unauthenticated JSON API. Supply a fixture to store tool_quality."
    ),
}


class JsonTransport(Protocol):
    def get_json(self, url: str) -> FetchedDocument:
        """Return parsed JSON for a public metadata URL."""


@dataclass(frozen=True)
class FetchedDocument:
    url: str
    data: Any
    fetched_at: str
    version: str | None = None


@dataclass
class FixtureTransport:
    """In-memory URL → JSON map used by unit tests (no network)."""

    payloads: Mapping[str, Any]
    fetched_at: str
    version: str = "fixture"

    def get_json(self, url: str) -> FetchedDocument:
        if url not in self.payloads:
            raise ModelMetadataError(f"fixture transport has no payload for {url}")
        return FetchedDocument(
            url=url, data=self.payloads[url], fetched_at=self.fetched_at, version=self.version
        )


class HttpxJsonTransport:
    """HTTPS GET restricted to the public metadata host allowlist."""

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def get_json(self, url: str) -> FetchedDocument:
        normalized = validate_upstream_url(url, allow_private_hosts=set())
        host = (urlsplit(normalized).hostname or "").rstrip(".").lower()
        if host not in ALLOWED_METADATA_HOSTS:
            raise ModelMetadataError(f"metadata host is not allowlisted: {host}")
        headers = {
            "Accept": "application/json",
            "User-Agent": "verdict-core/0.3.0 (BOD-108 metadata)",
        }
        with httpx.Client(
            transport=self._transport, timeout=self.timeout, follow_redirects=False
        ) as client:
            response = client.get(normalized, headers=headers)
            if response.is_redirect:
                raise ModelMetadataError("metadata redirect is not allowed")
            response.raise_for_status()
            content = response.content
            version_header = response.headers.get("ETag") or response.headers.get("Last-Modified")
        if len(content) > self.max_bytes:
            raise ModelMetadataError("metadata response exceeds max_bytes")
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ModelMetadataError("metadata response is not JSON") from exc
        version = version_header.strip().strip('"') if version_header else None
        return FetchedDocument(
            url=normalized, data=data, fetched_at=_iso(self._clock()), version=version or None
        )


def _bool_field(raw: object, provenance: FieldProvenance) -> ProvenancedField | None:
    if isinstance(raw, bool):
        return ProvenancedField(value=raw, provenance=provenance)
    return None


def _int_field(raw: object, provenance: FieldProvenance) -> ProvenancedField | None:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw < 0:
        return None
    return ProvenancedField(value=raw, provenance=provenance)


def _number_field(raw: object, provenance: FieldProvenance) -> ProvenancedField | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return ProvenancedField(value=float(raw), provenance=provenance)
    if isinstance(raw, float) and raw == raw:
        return ProvenancedField(value=raw, provenance=provenance)
    return None


def _vision_from_modalities(
    modalities: object, provenance: FieldProvenance
) -> ProvenancedField | None:
    if not isinstance(modalities, Mapping):
        return None
    inputs = modalities.get("input")
    if not isinstance(inputs, list):
        return None
    names = {item for item in inputs if isinstance(item, str)}
    return ProvenancedField(value="image" in names, provenance=provenance)


def _canonical_models_dev_id(provider_id: str, model_key: str, model: Mapping[str, Any]) -> str:
    stated = model.get("id")
    if isinstance(stated, str) and stated.strip():
        stated = stated.strip()
        if "/" in stated:
            return stated
        return f"{provider_id}/{stated}"
    if "/" in model_key:
        return model_key
    return f"{provider_id}/{model_key}"


def _provider_of(model_id: str) -> str:
    return model_id.split("/", 1)[0] if "/" in model_id else model_id


def parse_models_dev_api(
    document: Any, provenance: FieldProvenance
) -> dict[str, ModelMetadataRecord]:
    """Flatten models.dev api.json (provider → models) into canonical records."""
    if not isinstance(document, Mapping):
        raise ModelMetadataError("models.dev api.json must be an object")
    records: dict[str, ModelMetadataRecord] = {}
    for provider_key, provider in document.items():
        if not isinstance(provider_key, str) or not isinstance(provider, Mapping):
            continue
        provider_id = provider.get("id") if isinstance(provider.get("id"), str) else provider_key
        models = provider.get("models")
        if not isinstance(models, Mapping):
            continue
        for model_key, model in models.items():
            if not isinstance(model_key, str) or not isinstance(model, Mapping):
                continue
            record = _record_from_models_dev_model(
                _canonical_models_dev_id(str(provider_id), model_key, model),
                model,
                provenance,
                provider=str(provider_id),
            )
            records[record.id] = record
    return records


def parse_models_dev_models(
    document: Any, provenance: FieldProvenance
) -> dict[str, ModelMetadataRecord]:
    """Parse models.dev models.json (provider-agnostic id → facts).

    Callers should pass provenance with ``source=models.dev.models`` so unique-leaf
    joins (BOD-121) can distinguish these rows from api.json provider catalogs.
    """
    if not isinstance(document, Mapping):
        raise ModelMetadataError("models.dev models.json must be an object")
    records: dict[str, ModelMetadataRecord] = {}
    for key, model in document.items():
        if not isinstance(key, str) or not isinstance(model, Mapping):
            continue
        model_id = model.get("id") if isinstance(model.get("id"), str) else key
        record = _record_from_models_dev_model(str(model_id), model, provenance)
        records[record.id] = record
    return records


def _record_from_models_dev_model(
    model_id: str,
    model: Mapping[str, Any],
    provenance: FieldProvenance,
    *,
    provider: str | None = None,
) -> ModelMetadataRecord:
    cited = replace(provenance, raw_id=model_id)
    version = model.get("last_updated") or model.get("release_date")
    if isinstance(version, str) and version.strip() and not cited.version:
        cited = replace(cited, version=version.strip())
    elif isinstance(version, str) and version.strip():
        cited = replace(cited, version=cited.version)
    limit = model.get("limit") if isinstance(model.get("limit"), Mapping) else {}
    cost = model.get("cost") if isinstance(model.get("cost"), Mapping) else {}
    display = model.get("name") if isinstance(model.get("name"), str) else None
    caps = CapabilityCaps(
        tools=_bool_field(model.get("tool_call"), cited),
        vision=_vision_from_modalities(model.get("modalities"), cited),
        structured=_bool_field(model.get("structured_output"), cited),
        streaming=_bool_field(model.get("streaming"), cited),
        reasoning=_bool_field(model.get("reasoning"), cited),
        attachment=_bool_field(model.get("attachment"), cited),
        context=_int_field(limit.get("context") if isinstance(limit, Mapping) else None, cited),
        max_input=_int_field(limit.get("input") if isinstance(limit, Mapping) else None, cited),
        max_output=_int_field(limit.get("output") if isinstance(limit, Mapping) else None, cited),
        input_cost_per_million=_number_field(
            cost.get("input") if isinstance(cost, Mapping) else None, cited
        ),
        output_cost_per_million=_number_field(
            cost.get("output") if isinstance(cost, Mapping) else None, cited
        ),
    )
    return ModelMetadataRecord(
        id=model_id,
        caps=caps,
        scores=SoftScores(),
        provider=provider or _provider_of(model_id),
        display_name=display.strip() if isinstance(display, str) and display.strip() else None,
    )


def parse_litellm(document: Any, provenance: FieldProvenance) -> dict[str, ModelMetadataRecord]:
    """Parse LiteLLM model_prices_and_context_window.json into sparse records."""
    if not isinstance(document, Mapping):
        raise ModelMetadataError("LiteLLM registry must be an object")
    records: dict[str, ModelMetadataRecord] = {}
    for key, model in document.items():
        if not isinstance(key, str) or not isinstance(model, Mapping):
            continue
        if key.startswith("sample_") or key == "error":
            continue
        cited = replace(provenance, raw_id=key)
        context = model.get("max_input_tokens")
        if context is None:
            context = model.get("max_tokens")
        input_cost = model.get("input_cost_per_token")
        output_cost = model.get("output_cost_per_token")
        per_million_in = (
            None if not isinstance(input_cost, (int, float)) else float(input_cost) * 1_000_000
        )
        per_million_out = (
            None if not isinstance(output_cost, (int, float)) else float(output_cost) * 1_000_000
        )
        caps = CapabilityCaps(
            tools=_bool_field(model.get("supports_function_calling"), cited),
            vision=_bool_field(model.get("supports_vision"), cited),
            structured=_bool_field(model.get("supports_response_schema"), cited),
            streaming=_bool_field(model.get("supports_streaming"), cited),
            context=_int_field(context, cited),
            max_input=_int_field(model.get("max_input_tokens"), cited),
            max_output=_int_field(model.get("max_output_tokens") or model.get("max_tokens"), cited),
            input_cost_per_million=_number_field(per_million_in, cited),
            output_cost_per_million=_number_field(per_million_out, cited),
        )
        provider = model.get("litellm_provider")
        records[key] = ModelMetadataRecord(
            id=key,
            caps=caps,
            scores=SoftScores(),
            provider=provider if isinstance(provider, str) else _provider_of(key),
        )
    return records


def _fill_missing(
    primary: ProvenancedField | None, secondary: ProvenancedField | None
) -> tuple[ProvenancedField | None, bool]:
    """Keep primary when present. Secondary fills only unknown. Conflict if both differ."""
    if primary is not None:
        if secondary is not None and primary.value != secondary.value:
            return primary, True
        return primary, False
    return secondary, False


def merge_litellm_secondary(
    records: MutableMapping[str, ModelMetadataRecord], litellm: Mapping[str, ModelMetadataRecord]
) -> list[tuple[str, str]]:
    """Fill unknown caps from LiteLLM. Never override models.dev. Never invent."""
    conflicts: list[tuple[str, str]] = []
    for record in list(records.values()):
        secondary = _litellm_match(record.id, litellm)
        if secondary is None:
            continue
        caps = record.caps
        updates: dict[str, ProvenancedField | None] = {}
        for name in (
            "tools",
            "vision",
            "structured",
            "streaming",
            "reasoning",
            "attachment",
            "context",
            "max_input",
            "max_output",
            "input_cost_per_million",
            "output_cost_per_million",
        ):
            kept, conflicted = _fill_missing(getattr(caps, name), getattr(secondary.caps, name))
            updates[name] = kept
            if conflicted:
                conflicts.append((record.id, name))
        records[record.id] = ModelMetadataRecord(
            id=record.id,
            caps=CapabilityCaps(**updates),
            scores=record.scores,
            provider=record.provider,
            display_name=record.display_name,
            omniroute_ids=record.omniroute_ids,
        )
    return conflicts


def _litellm_match(
    models_dev_id: str, litellm: Mapping[str, ModelMetadataRecord]
) -> ModelMetadataRecord | None:
    if models_dev_id in litellm:
        return litellm[models_dev_id]
    suffix = models_dev_id.split("/", 1)[-1]
    if suffix in litellm:
        return litellm[suffix]
    return None


def parse_soft_score_table(
    document: Any,
    *,
    source: str,
    provenance: FieldProvenance,
    id_keys: tuple[str, ...] = ("id", "model", "slug"),
    field_map: Mapping[str, str],
) -> dict[str, SoftScores]:
    """Parse a generic `{data: [{id, ...scores}]}` fixture. Unknown fields stay null."""
    rows: list[Any]
    if isinstance(document, Mapping) and isinstance(document.get("data"), list):
        rows = document["data"]
    elif isinstance(document, list):
        rows = document
    else:
        raise ModelMetadataError(f"{source} payload must be a list or {{data: []}}")
    scores: dict[str, SoftScores] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_id = None
        for key in id_keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                raw_id = value.strip()
                break
        if raw_id is None:
            continue
        cited = replace(provenance, source=source, raw_id=raw_id)
        kwargs: dict[str, ProvenancedField | None] = {
            name: None
            for name in (
                "aa_intelligence",
                "aa_coding",
                "aa_agentic",
                "aa_latency",
                "arena_elo",
                "open_llm",
                "bfcl",
            )
        }
        for src_field, dest in field_map.items():
            parsed = _number_field(row.get(src_field), cited)
            if parsed is None and isinstance(row.get("evaluations"), Mapping):
                parsed = _number_field(row["evaluations"].get(src_field), cited)
            if dest in kwargs:
                kwargs[dest] = parsed
        scores[raw_id] = SoftScores(**kwargs)
    return scores


AA_FIELD_MAP = {
    "intelligence_index": "aa_intelligence",
    "coding_index": "aa_coding",
    "agentic_index": "aa_agentic",
    "median_time_to_first_token": "aa_latency",
}
ARENA_FIELD_MAP = {"elo": "arena_elo"}
OPEN_LLM_FIELD_MAP = {"average": "open_llm"}
BFCL_FIELD_MAP = {"overall_accuracy": "bfcl"}


def apply_soft_scores(
    records: MutableMapping[str, ModelMetadataRecord], scores: Mapping[str, SoftScores]
) -> int:
    """Attach scores only when the source id equals a stored models.dev id. No fuzzy join."""
    applied = 0
    by_id = records
    for raw_id, incoming in scores.items():
        target = by_id.get(raw_id)
        if target is None:
            continue
        merged = SoftScores(
            aa_intelligence=target.scores.aa_intelligence or incoming.aa_intelligence,
            aa_coding=target.scores.aa_coding or incoming.aa_coding,
            aa_agentic=target.scores.aa_agentic or incoming.aa_agentic,
            aa_latency=target.scores.aa_latency or incoming.aa_latency,
            arena_elo=target.scores.arena_elo or incoming.arena_elo,
            open_llm=target.scores.open_llm or incoming.open_llm,
            bfcl=target.scores.bfcl or incoming.bfcl,
        )
        by_id[raw_id] = ModelMetadataRecord(
            id=target.id,
            caps=target.caps,
            scores=merged,
            provider=target.provider,
            display_name=target.display_name,
            omniroute_ids=target.omniroute_ids,
        )
        applied += 1
    return applied


def skipped_p1(source: str) -> SourceStatus:
    return SourceStatus(source=source, status="skipped", reason=P1_SKIP_REASONS[source])
