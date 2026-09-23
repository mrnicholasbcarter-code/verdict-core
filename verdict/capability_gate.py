"""Capability hard gate against the Core metadata store (BOD-100).

Requirements come from the request/task envelope. Matches use Core's
independently fetched store — never OmniRoute catalog rows as metadata SoT.
Required + null/unknown/stale/false is a named drop for free and paid alike.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from verdict.evidence import request_features
from verdict.free_tier_admit import (
    REASON_CAPABILITY_MISMATCH,
    REASON_REQUIRED_UNKNOWN,
    REASON_STALE,
    REASON_UNMAPPED,
    FreeTierAdmitReceipt,
    NamedDrop,
)
from verdict.metadata.mapping import IdentityMap
from verdict.metadata.records import (
    DROP_MAP_TARGET_MISSING,
    DROP_REQUIRED_UNKNOWN,
    DROP_STALE,
    DROP_UNMAPPED,
    MetadataLookup,
    ModelMetadataError,
    ProvenancedField,
    resolve_required_name,
)
from verdict.metadata.store import MetadataSnapshot, lookup_omniroute_id

_BOOL_CAPS = frozenset({"tools", "vision", "structured", "reasoning", "attachment", "streaming"})
_CONTEXT_CAP = "context"

_LOOKUP_REASON: dict[str, str] = {
    DROP_UNMAPPED: REASON_UNMAPPED,
    DROP_MAP_TARGET_MISSING: REASON_UNMAPPED,
    DROP_REQUIRED_UNKNOWN: REASON_REQUIRED_UNKNOWN,
    DROP_STALE: REASON_STALE,
}


@dataclass(frozen=True)
class TaskRequirements:
    """Hard caps the capability gate must observe. Empty means no extra drops."""

    names: tuple[str, ...] = ()
    min_context: int | None = None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"names": list(self.names), "reasons": list(self.reasons)}
        if self.min_context is not None:
            payload["min_context"] = self.min_context
        return payload


def derive_requirements(
    task: str, context: Mapping[str, Any] | None = None, *, planner_capabilities: Sequence[str] = ()
) -> TaskRequirements:
    """Derive tools/vision/structured/context requirements. Invent nothing."""
    names: list[str] = []
    reasons: list[str] = []
    min_context: int | None = None
    payload = dict(context) if isinstance(context, Mapping) else {}
    features = request_features(payload) if payload else {}

    def _add(name: str, reason: str) -> None:
        if name not in names:
            names.append(name)
            reasons.append(reason)

    if payload.get("tools_required") is True or payload.get("require_tools") is True:
        _add("tools", "request.tools_required")
    if features.get("tools") is True:
        _add("tools", "request.tools")
    if payload.get("vision_required") is True or features.get("vision") is True:
        _add("vision", "request.vision")
    if payload.get("reasoning_required") is True or payload.get("require_reasoning") is True:
        _add("reasoning", "request.reasoning")
    if payload.get("streaming_required") is True or features.get("stream") is True:
        _add("streaming", "request.streaming")
    structured = payload.get("structured_output_required") is True or payload.get(
        "require_structured_output"
    )
    fmt = features.get("response_format")
    if structured or fmt in {"json_schema", "json_object"}:
        _add("structured", "request.structured_output")
    for cap in planner_capabilities:
        mapped = {
            "tool-calling": "tools",
            "tools": "tools",
            "vision": "vision",
            "structured_output": "structured",
            "structured": "structured",
            "reasoning": "reasoning",
            "streaming": "streaming",
        }.get(str(cap).strip().lower())
        if mapped:
            _add(mapped, f"planner:{cap}")
    raw_min = payload.get("min_context") or payload.get("min_context_tokens")
    if isinstance(raw_min, int) and not isinstance(raw_min, bool) and raw_min > 0:
        min_context = raw_min
        reasons.append(f"request.min_context={raw_min}")
    _ = task  # task text is not a capability oracle
    return TaskRequirements(names=tuple(names), min_context=min_context, reasons=tuple(reasons))


def _field_true(field: ProvenancedField | None) -> bool | None:
    if field is None:
        return None
    value = field.value
    if isinstance(value, bool):
        return value
    return None


def _match_row(
    identity_id: str, lookup: MetadataLookup, requirements: TaskRequirements
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "model": identity_id,
        "admitted": lookup.admitted_for_caps and lookup.drop is None,
        "metadata_id": None if lookup.record is None else lookup.record.id,
        "provenance": lookup.provenance_for_receipt(requirements.names or None),
    }
    if lookup.drop is not None:
        row["drop"] = lookup.drop.to_dict()
        row["admitted"] = False
        return row
    record = lookup.record
    if record is None:
        row["admitted"] = False
        row["drop"] = {"reason": REASON_UNMAPPED, "omniroute_id": identity_id}
        return row
    missing: list[str] = []
    mismatched: list[str] = []
    for name in requirements.names:
        canonical = resolve_required_name(name)
        field = record.caps.field(canonical)
        if field is None:
            missing.append(canonical)
            continue
        if canonical in _BOOL_CAPS:
            observed = _field_true(field)
            if observed is None:
                missing.append(canonical)
            elif observed is False:
                mismatched.append(canonical)
    if requirements.min_context is not None:
        context_field = record.caps.field(_CONTEXT_CAP)
        if context_field is None:
            missing.append(_CONTEXT_CAP)
        else:
            try:
                window = int(context_field.value)
            except (TypeError, ValueError):
                missing.append(_CONTEXT_CAP)
            else:
                if window < requirements.min_context:
                    mismatched.append(_CONTEXT_CAP)
                    row["context"] = window
    if missing:
        row["admitted"] = False
        row["drop"] = {
            "reason": REASON_REQUIRED_UNKNOWN,
            "omniroute_id": identity_id,
            "missing_fields": missing,
        }
        return row
    if mismatched:
        row["admitted"] = False
        row["drop"] = {
            "reason": REASON_CAPABILITY_MISMATCH,
            "omniroute_id": identity_id,
            "missing_fields": mismatched,
        }
        return row
    row["admitted"] = True
    return row


def gate_capability(
    receipt: FreeTierAdmitReceipt,
    requirements: TaskRequirements,
    *,
    snapshot: MetadataSnapshot | None,
    identity_map: IdentityMap | None = None,
    now: Any = None,
    max_age: timedelta | None = None,
) -> FreeTierAdmitReceipt:
    """Drop admitted identities that fail required Core metadata caps.

    No requirements → no extra drops (store may be absent). Required fields with
    no store are named ``required_unknown`` drops — never an optimistic yes.
    """
    if not requirements.names and requirements.min_context is None:
        return replace(receipt, requirements=(), capability_matches=())

    matches: list[dict[str, Any]] = []
    exclusions = list(receipt.exclusions)
    kept: list[str] = []
    required_names = requirements.names
    for identity_id in receipt.admitted:
        if snapshot is None:
            row = {
                "model": identity_id,
                "admitted": False,
                "drop": {
                    "reason": REASON_REQUIRED_UNKNOWN,
                    "omniroute_id": identity_id,
                    "detail": "Core metadata store unavailable",
                    "missing_fields": list(required_names),
                },
                "provenance": {},
            }
        else:
            try:
                lookup = lookup_omniroute_id(
                    snapshot,
                    identity_id,
                    required=required_names,
                    identity_map=identity_map,
                    now=now,
                    max_age=max_age,
                )
            except ModelMetadataError as exc:
                row = {
                    "model": identity_id,
                    "admitted": False,
                    "drop": {
                        "reason": REASON_REQUIRED_UNKNOWN,
                        "omniroute_id": identity_id,
                        "detail": str(exc),
                    },
                    "provenance": {},
                }
                matches.append(row)
                exclusions.append(NamedDrop(identity_id, REASON_REQUIRED_UNKNOWN, str(exc)))
                continue
            row = _match_row(identity_id, lookup, requirements)
        matches.append(row)
        if row.get("admitted") is True:
            kept.append(identity_id)
            continue
        raw_drop = row.get("drop")
        drop: dict[str, Any] = raw_drop if isinstance(raw_drop, dict) else {}
        reason = str(drop.get("reason") or REASON_REQUIRED_UNKNOWN)
        mapped = _LOOKUP_REASON.get(reason, reason)
        if mapped not in {
            REASON_CAPABILITY_MISMATCH,
            REASON_REQUIRED_UNKNOWN,
            REASON_UNMAPPED,
            REASON_STALE,
        }:
            mapped = REASON_REQUIRED_UNKNOWN
        missing = drop.get("missing_fields") if isinstance(drop, dict) else None
        detail = drop.get("detail") if isinstance(drop, dict) else None
        if isinstance(missing, list) and missing:
            detail = ",".join(str(item) for item in missing)
        exclusions.append(
            NamedDrop(identity_id, mapped, detail if isinstance(detail, str) else None)
        )

    remaining = tuple(kept)
    chosen = (
        receipt.chosen if receipt.chosen in remaining else (remaining[0] if remaining else None)
    )
    return replace(
        receipt,
        admitted=remaining,
        exclusions=tuple(exclusions),
        chosen=chosen,
        empty_intersection=chosen is None,
        requirements=requirements.names
        + (() if requirements.min_context is None else (f"context>={requirements.min_context}",)),
        capability_matches=tuple(matches),
    )
