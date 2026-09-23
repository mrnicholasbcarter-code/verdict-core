"""Core-owned model metadata records with per-field provenance (BOD-108).

OmniRoute is inventory/execute/health only. Every stored cap or soft score
cites ``source`` plus ``version`` and/or ``fetched_at``. Soft ranks are stored
only when a fetcher actually returned them — never invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, cast

METADATA_SCHEMA_VERSION = "1"

SourceStatusKind = Literal["ok", "skipped", "error"]
DropReason = Literal["unmapped", "required_unknown", "stale", "map_target_missing"]

SOURCE_MODELS_DEV = "models.dev"
SOURCE_MODELS_DEV_MODELS = "models.dev.models"
SOURCE_LITELLM = "litellm"
SOURCE_ARTIFICIAL_ANALYSIS = "artificial_analysis"
SOURCE_ARENA_ELO = "arena_elo"
SOURCE_OPEN_LLM = "open_llm_leaderboard"
SOURCE_BFCL = "bfcl"

DROP_UNMAPPED: DropReason = "unmapped"
DROP_REQUIRED_UNKNOWN: DropReason = "required_unknown"
DROP_STALE: DropReason = "stale"
DROP_MAP_TARGET_MISSING: DropReason = "map_target_missing"

CAP_FIELDS = (
    "tools",
    "vision",
    "structured",
    "reasoning",
    "attachment",
    "context",
    "max_input",
    "max_output",
    "input_cost_per_million",
    "output_cost_per_million",
)
SCORE_FIELDS = (
    "aa_intelligence",
    "aa_coding",
    "aa_agentic",
    "aa_latency",
    "arena_elo",
    "open_llm",
    "bfcl",
)
REQUIRED_CAP_ALIASES = {
    "tools": "tools",
    "tool_call": "tools",
    "supports_tools": "tools",
    "vision": "vision",
    "supports_vision": "vision",
    "structured": "structured",
    "structured_output": "structured",
    "supports_structured": "structured",
    "reasoning": "reasoning",
    "attachment": "attachment",
    "context": "context",
    "context_limit": "context",
    "context_window": "context",
}


class ModelMetadataError(ValueError):
    """Raised when a metadata record or snapshot violates its contract."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ModelMetadataError("timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelMetadataError(f"{field_name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class FieldProvenance:
    """Source citation for one stored field."""

    source: str
    fetched_at: str | None = None
    version: str | None = None
    raw_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", _non_empty(self.source, "provenance.source"))
        version = self.version.strip() if isinstance(self.version, str) else self.version
        fetched_at = (
            self.fetched_at.strip() if isinstance(self.fetched_at, str) else self.fetched_at
        )
        raw_id = self.raw_id.strip() if isinstance(self.raw_id, str) else self.raw_id
        if version is not None and not version:
            raise ModelMetadataError("provenance.version must be non-empty when supplied")
        if fetched_at is not None and not fetched_at:
            raise ModelMetadataError("provenance.fetched_at must be non-empty when supplied")
        if raw_id is not None and not raw_id:
            raise ModelMetadataError("provenance.raw_id must be non-empty when supplied")
        if not version and not fetched_at:
            raise ModelMetadataError("provenance requires version or fetched_at")
        object.__setattr__(self, "version", version)
        object.__setattr__(self, "fetched_at", fetched_at)
        object.__setattr__(self, "raw_id", raw_id)

    def to_dict(self) -> dict[str, str]:
        payload = {"source": self.source}
        if self.fetched_at:
            payload["fetched_at"] = self.fetched_at
        if self.version:
            payload["version"] = self.version
        if self.raw_id:
            payload["raw_id"] = self.raw_id
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> FieldProvenance:
        if not isinstance(value, Mapping):
            raise ModelMetadataError("provenance must be an object")
        return cls(
            source=str(value.get("source", "")),
            fetched_at=value.get("fetched_at") if value.get("fetched_at") is not None else None,
            version=value.get("version") if value.get("version") is not None else None,
            raw_id=value.get("raw_id") if value.get("raw_id") is not None else None,
        )


@dataclass(frozen=True)
class ProvenancedField:
    """A stored value that cannot exist without provenance."""

    value: bool | int | float
    provenance: FieldProvenance

    def __post_init__(self) -> None:
        if isinstance(self.value, bool):
            return
        if isinstance(self.value, int):
            return
        if isinstance(self.value, float) and self.value == self.value:  # not NaN
            return
        raise ModelMetadataError("provenanced value must be bool, int, or finite float")

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "provenance": self.provenance.to_dict()}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProvenancedField:
        if not isinstance(value, Mapping) or "value" not in value or "provenance" not in value:
            raise ModelMetadataError("provenanced field requires value and provenance")
        raw = value["value"]
        if isinstance(raw, bool):
            parsed: bool | int | float = raw
        elif isinstance(raw, (int, float)):
            parsed = raw
        else:
            raise ModelMetadataError("provenanced value must be bool, int, or number")
        return cls(value=parsed, provenance=FieldProvenance.from_dict(value["provenance"]))


def _optional_field(value: object, field_name: str) -> ProvenancedField | None:
    if value is None:
        return None
    if isinstance(value, ProvenancedField):
        return value
    if isinstance(value, Mapping):
        return ProvenancedField.from_dict(value)
    raise ModelMetadataError(f"{field_name} must be a provenanced field or null")


@dataclass(frozen=True)
class CapabilityCaps:
    """Hard capability facts. Null means unknown — never treat as false."""

    tools: ProvenancedField | None = None
    vision: ProvenancedField | None = None
    structured: ProvenancedField | None = None
    streaming: ProvenancedField | None = None
    reasoning: ProvenancedField | None = None
    attachment: ProvenancedField | None = None
    context: ProvenancedField | None = None
    max_input: ProvenancedField | None = None
    max_output: ProvenancedField | None = None
    input_cost_per_million: ProvenancedField | None = None
    output_cost_per_million: ProvenancedField | None = None

    def field(self, name: str) -> ProvenancedField | None:
        canonical = REQUIRED_CAP_ALIASES.get(name, name)
        if canonical not in CAP_FIELDS:
            raise ModelMetadataError(f"unknown capability field: {name}")
        return cast(ProvenancedField | None, getattr(self, canonical))

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name in CAP_FIELDS:
            item = getattr(self, name)
            if item is not None:
                payload[name] = item.to_dict()
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> CapabilityCaps:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ModelMetadataError("caps must be an object")
        unknown = set(value) - set(CAP_FIELDS)
        if unknown:
            raise ModelMetadataError(f"unknown cap field: {sorted(unknown)[0]}")
        return cls(
            **{name: _optional_field(value.get(name), f"caps.{name}") for name in CAP_FIELDS}
        )


@dataclass(frozen=True)
class SoftScores:
    """Nullable soft ranks. Absent means not fetched — never invented."""

    aa_intelligence: ProvenancedField | None = None
    aa_coding: ProvenancedField | None = None
    aa_agentic: ProvenancedField | None = None
    aa_latency: ProvenancedField | None = None
    arena_elo: ProvenancedField | None = None
    open_llm: ProvenancedField | None = None
    bfcl: ProvenancedField | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for name in SCORE_FIELDS:
            item = getattr(self, name)
            if item is not None:
                payload[name] = item.to_dict()
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> SoftScores:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ModelMetadataError("scores must be an object")
        unknown = set(value) - set(SCORE_FIELDS)
        if unknown:
            raise ModelMetadataError(f"unknown score field: {sorted(unknown)[0]}")
        return cls(
            **{name: _optional_field(value.get(name), f"scores.{name}") for name in SCORE_FIELDS}
        )


@dataclass(frozen=True)
class ModelMetadataRecord:
    """One models.dev identity with optional OmniRoute aliases and provenance."""

    id: str
    caps: CapabilityCaps = field(default_factory=CapabilityCaps)
    scores: SoftScores = field(default_factory=SoftScores)
    provider: str | None = None
    display_name: str | None = None
    omniroute_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _non_empty(self.id, "id"))
        ids = tuple(_non_empty(item, "omniroute_ids[]") for item in self.omniroute_ids)
        object.__setattr__(self, "omniroute_ids", ids)
        if self.provider is not None:
            object.__setattr__(self, "provider", _non_empty(self.provider, "provider"))
        if self.display_name is not None:
            object.__setattr__(self, "display_name", _non_empty(self.display_name, "display_name"))

    def with_omniroute_ids(self, extra: tuple[str, ...]) -> ModelMetadataRecord:
        merged = tuple(dict.fromkeys((*self.omniroute_ids, *extra)))
        return ModelMetadataRecord(
            id=self.id,
            caps=self.caps,
            scores=self.scores,
            provider=self.provider,
            display_name=self.display_name,
            omniroute_ids=merged,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "caps": self.caps.to_dict(),
            "scores": self.scores.to_dict(),
        }
        if self.provider:
            payload["provider"] = self.provider
        if self.display_name:
            payload["display_name"] = self.display_name
        if self.omniroute_ids:
            payload["omniroute_ids"] = list(self.omniroute_ids)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModelMetadataRecord:
        if not isinstance(value, Mapping):
            raise ModelMetadataError("record must be an object")
        omni = value.get("omniroute_ids") or ()
        if not isinstance(omni, (list, tuple)):
            raise ModelMetadataError("omniroute_ids must be an array")
        return cls(
            id=str(value.get("id", "")),
            caps=CapabilityCaps.from_dict(value.get("caps")),
            scores=SoftScores.from_dict(value.get("scores")),
            provider=value.get("provider"),
            display_name=value.get("display_name"),
            omniroute_ids=tuple(str(item) for item in omni),
        )


@dataclass(frozen=True)
class MetadataDrop:
    """Named drop for BOD-100: unmapped, required-unknown, stale, or missing target."""

    omniroute_id: str
    reason: DropReason
    detail: str | None = None
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "omniroute_id", _non_empty(self.omniroute_id, "omniroute_id"))
        if self.reason not in {
            DROP_UNMAPPED,
            DROP_REQUIRED_UNKNOWN,
            DROP_STALE,
            DROP_MAP_TARGET_MISSING,
        }:
            raise ModelMetadataError(f"invalid drop reason: {self.reason}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"omniroute_id": self.omniroute_id, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        if self.missing_fields:
            payload["missing_fields"] = list(self.missing_fields)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MetadataDrop:
        missing = value.get("missing_fields") or ()
        reason_raw = value.get("reason", DROP_UNMAPPED)
        if reason_raw not in {
            DROP_UNMAPPED,
            DROP_REQUIRED_UNKNOWN,
            DROP_STALE,
            DROP_MAP_TARGET_MISSING,
        }:
            raise ModelMetadataError("invalid drop reason")
        return cls(
            omniroute_id=str(value.get("omniroute_id", "")),
            reason=cast(DropReason, reason_raw),
            detail=value.get("detail") if isinstance(value.get("detail"), str) else None,
            missing_fields=tuple(str(item) for item in missing),
        )


@dataclass(frozen=True)
class SourceStatus:
    """Refresh outcome for one public source."""

    source: str
    status: SourceStatusKind
    url: str | None = None
    fetched_at: str | None = None
    version: str | None = None
    reason: str | None = None
    record_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"source": self.source, "status": self.status}
        if self.url:
            payload["url"] = self.url
        if self.fetched_at:
            payload["fetched_at"] = self.fetched_at
        if self.version:
            payload["version"] = self.version
        if self.reason:
            payload["reason"] = self.reason
        if self.record_count is not None:
            payload["record_count"] = self.record_count
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceStatus:
        status = value.get("status", "error")
        if status not in {"ok", "skipped", "error"}:
            raise ModelMetadataError("source status is invalid")
        return cls(
            source=_non_empty(value.get("source", ""), "source"),
            status=status,
            url=value.get("url"),
            fetched_at=value.get("fetched_at"),
            version=value.get("version"),
            reason=value.get("reason"),
            record_count=value.get("record_count"),
        )


@dataclass(frozen=True)
class FieldConflict:
    """LiteLLM disagreed with models.dev; primary (models.dev) was kept."""

    id: str
    field: str
    primary_source: str
    secondary_source: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {
            "id": self.id,
            "field": self.field,
            "primary_source": self.primary_source,
            "secondary_source": self.secondary_source,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class MetadataLookup:
    """Read API for later BOD-100 capability gating."""

    omniroute_id: str
    record: ModelMetadataRecord | None
    drop: MetadataDrop | None

    @property
    def admitted_for_caps(self) -> bool:
        return self.drop is None and self.record is not None

    def provenance_for_receipt(
        self, fields: tuple[str, ...] | None = None
    ) -> dict[str, dict[str, str]]:
        """Cite source+version for each cap a later receipt used."""
        if self.record is None:
            return {}
        names = fields if fields is not None else CAP_FIELDS
        cited: dict[str, dict[str, str]] = {}
        for name in names:
            canonical = REQUIRED_CAP_ALIASES.get(name, name)
            item = None
            if canonical in CAP_FIELDS:
                item = self.record.caps.field(canonical)
            elif canonical in SCORE_FIELDS:
                item = getattr(self.record.scores, canonical)
            if item is not None:
                cited[canonical] = item.provenance.to_dict()
        return cited

    def to_dict(self) -> dict[str, Any]:
        return {
            "omniroute_id": self.omniroute_id,
            "admitted_for_caps": self.admitted_for_caps,
            "record": None if self.record is None else self.record.to_dict(),
            "drop": None if self.drop is None else self.drop.to_dict(),
            "provenance": self.provenance_for_receipt(),
        }


def resolve_required_name(name: str) -> str:
    """Map a caller-facing requirement name onto a stored cap field."""
    canonical = REQUIRED_CAP_ALIASES.get(name, name)
    if canonical not in CAP_FIELDS:
        raise ModelMetadataError(f"unknown required field: {name}")
    return canonical
