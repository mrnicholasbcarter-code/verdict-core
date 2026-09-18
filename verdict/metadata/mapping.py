"""Explicit OmniRoute inventory id → models.dev id mapping (BOD-108).

Unmapped ids are a named drop. Identity equality (same string) is accepted
as an explicit join; fuzzy or related-model substitution is not.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.metadata.records import ModelMetadataError, _non_empty
from verdict.security import fingerprint_text

DEFAULT_MAPPING_RELATIVE = Path("data") / "omniroute_models_dev_map.json"


def default_mapping_path() -> Path:
    return Path(__file__).resolve().parent.parent / DEFAULT_MAPPING_RELATIVE


@dataclass(frozen=True)
class IdentityMap:
    """Bidirectional explicit join table."""

    omniroute_to_models_dev: dict[str, str]
    path: Path | None = None
    digest: str | None = None

    def models_dev_id_for(self, omniroute_id: str) -> str | None:
        key = _non_empty(omniroute_id, "omniroute_id")
        mapped = self.omniroute_to_models_dev.get(key)
        if mapped:
            return mapped
        return None

    def omniroute_ids_for(self, models_dev_id: str) -> tuple[str, ...]:
        target = _non_empty(models_dev_id, "models_dev_id")
        return tuple(
            omni_id
            for omni_id, models_id in self.omniroute_to_models_dev.items()
            if models_id == target
        )

    def to_summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"entry_count": len(self.omniroute_to_models_dev)}
        if self.path is not None:
            payload["path"] = str(self.path)
        if self.digest:
            payload["digest"] = self.digest
        return payload


def load_identity_map(path: Path | str | None = None) -> IdentityMap:
    resolved = Path(path) if path is not None else default_mapping_path()
    raw = resolved.read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ModelMetadataError(f"identity map is not JSON: {resolved}") from exc
    if not isinstance(document, Mapping):
        raise ModelMetadataError("identity map must be an object")
    entries = document.get("entries")
    if not isinstance(entries, list):
        raise ModelMetadataError("identity map.entries must be an array")
    mapping: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ModelMetadataError(f"identity map.entries[{index}] must be an object")
        omni = _non_empty(entry.get("omniroute_id", ""), f"entries[{index}].omniroute_id")
        models_dev = _non_empty(entry.get("models_dev_id", ""), f"entries[{index}].models_dev_id")
        existing = mapping.get(omni)
        if existing is not None and existing != models_dev:
            raise ModelMetadataError(f"duplicate omniroute_id with conflicting target: {omni}")
        mapping[omni] = models_dev
    digest = fingerprint_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")), length=64
    )
    return IdentityMap(omniroute_to_models_dev=mapping, path=resolved, digest=digest)
