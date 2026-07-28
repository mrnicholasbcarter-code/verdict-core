"""Deterministic, credential-free statistics for catalog qualification."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any


def provider_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    values = Counter(provider(row) for row in rows)
    return dict(sorted(values.items()))


def provider(row: Mapping[str, Any]) -> str:
    value = row.get("provider") or row.get("owned_by")
    if isinstance(value, str) and value.strip():
        return value.strip()
    model_id = row.get("id")
    if isinstance(model_id, str) and "/" in model_id:
        return model_id.split("/", 1)[0]
    return "unknown"


def capability_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    values: Counter[str] = Counter()
    for row in rows:
        capabilities = row.get("capabilities")
        if isinstance(capabilities, Mapping):
            values.update(str(key) for key, value in capabilities.items() if value is True)
    return dict(sorted(values.items()))


def profile_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    profiles = Counter[str]()
    for row in rows:
        model_id = str(row.get("id", "")).lower()
        name = str(row.get("name", "")).lower()
        evidence = f"{model_id} {name}"
        capabilities = row.get("capabilities")
        caps = capabilities if isinstance(capabilities, Mapping) else {}
        if any(token in evidence for token in ("code", "coder", "coding", "dev", "mimo")):
            profiles["coding"] += 1
        if caps.get("reasoning") is True or caps.get("thinking") is True:
            profiles["reasoning"] += 1
        if any(
            token in evidence
            for token in ("flash", "haiku", "mini", "small", "fast", "lite", "nano", "instant")
        ):
            profiles["fast"] += 1
        context_length = number(row.get("context_length"))
        if context_length is not None and context_length >= 1_000_000:
            profiles["long_context"] += 1
        if caps.get("tool_calling") is True:
            profiles["tool_use"] += 1
        if caps.get("structured_output") is True:
            profiles["structured_output"] += 1
        if ":free" in model_id:
            profiles["free_tier"] += 1
    return {key: profiles[key] for key in sorted(profiles)}


def numeric_stats(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, int | None]:
    values = [value for row in rows if (value := number(row.get(key))) is not None]
    return {
        "rows": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def number(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def duplicate_classification(rows: Sequence[Mapping[str, Any]]) -> str:
    types = {row.get("type") for row in rows}
    roots = {row.get("root") for row in rows}
    if len(types) > 1 or len(roots) > 1 or any(row.get("type") for row in rows):
        return "multi-projection"
    return "duplicate-metadata"


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
