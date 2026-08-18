"""Credential-safe OmniRoute catalog qualification and provenance."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from verdict.memory_plane import MemoryPlane, MemoryRecord
from verdict.omniroute_catalog_stats import (
    canonical_json,
    capability_counts,
    duplicate_classification,
    numeric_stats,
    profile_counts,
    provider,
    provider_counts,
    utc,
)

CATALOG_QUALIFICATION_VERSION = "1"
DEFAULT_CATALOG_FRESHNESS_SECONDS = 3_600
DEFAULT_EXPECTED_ROW_COUNT = 3_977
MAX_CATALOG_PROBE_SAMPLE = 16
CatalogStatus = Literal["qualified", "partial", "stale", "unknown"]


class CatalogQualificationError(ValueError):
    """Raised when an OmniRoute catalog cannot be interpreted safely."""


@dataclass(frozen=True)
class CatalogSnapshot:
    """Sanitized, deterministic catalog facts; never the raw catalog rows."""

    source_url: str
    captured_at: datetime
    freshness_seconds: int
    schema: str
    payload_hash: str
    row_count: int
    unique_id_count: int
    malformed_row_count: int
    expected_row_count: int
    duplicate_ids: tuple[dict[str, Any], ...]
    provider_counts: dict[str, int]
    capability_counts: dict[str, int]
    profile_counts: dict[str, int]
    context_length: dict[str, int | None]
    max_output_tokens: dict[str, int | None]
    catalog_version: str | None = None

    @property
    def fresh_until(self) -> datetime:
        return self.captured_at + timedelta(seconds=self.freshness_seconds)

    @property
    def duplicate_row_delta(self) -> int:
        return self.row_count - self.unique_id_count

    @property
    def target_row_delta(self) -> int:
        return self.row_count - self.expected_row_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualification_version": CATALOG_QUALIFICATION_VERSION,
            "source_url": self.source_url,
            "captured_at": self.captured_at.isoformat(),
            "fresh_until": self.fresh_until.isoformat(),
            "freshness_seconds": self.freshness_seconds,
            "schema": self.schema,
            "catalog_version": self.catalog_version,
            "payload_hash": self.payload_hash,
            "reconciliation": {
                "expected_rows": self.expected_row_count,
                "rows": self.row_count,
                "unique_ids": self.unique_id_count,
                "target_row_delta": self.target_row_delta,
                "duplicate_row_delta": self.duplicate_row_delta,
                "malformed_rows": self.malformed_row_count,
                "duplicate_ids": [dict(item) for item in self.duplicate_ids],
            },
            "provider_counts": dict(self.provider_counts),
            "capability_counts": dict(self.capability_counts),
            "profile_counts": dict(self.profile_counts),
            "context_length": dict(self.context_length),
            "max_output_tokens": dict(self.max_output_tokens),
        }


@dataclass(frozen=True)
class CatalogQualificationReport:
    """Fail-closed result of qualifying one catalog response."""

    status: CatalogStatus
    snapshot: CatalogSnapshot | None
    errors: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "qualified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": "omniroute-catalog-qualification",
            "qualification_version": CATALOG_QUALIFICATION_VERSION,
            "status": self.status,
            "passed": self.passed,
            "errors": list(self.errors),
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }


@dataclass(frozen=True)
class CatalogProjectionReconciliation:
    """Sanitized comparison of the public and management projections."""

    status: Literal["consistent", "contradictory", "unknown"]
    public_status: CatalogStatus
    management_status: CatalogStatus
    compared_fields: tuple[str, ...]
    mismatches: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "consistent" and not self.mismatches

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "public_status": self.public_status,
            "management_status": self.management_status,
            "compared_fields": list(self.compared_fields),
            "mismatches": list(self.mismatches),
        }


@dataclass(frozen=True)
class ProbeQualification:
    """Sanitized liveness results for an explicitly bounded probe sample."""

    captured_at: datetime
    attempted: int
    ready: int
    non_ready: int
    statuses: dict[str, int]
    error_classes: dict[str, int]
    results: tuple[dict[str, Any], ...]
    catalog_payload_hash: str | None = None
    selected_model_ids: tuple[str, ...] = ()
    diagnostics: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "captured_at": self.captured_at.isoformat(),
            "catalog_payload_hash": self.catalog_payload_hash,
            "attempted": self.attempted,
            "ready": self.ready,
            "non_ready": self.non_ready,
            "statuses": dict(self.statuses),
            "error_classes": dict(self.error_classes),
            "selected_model_ids": list(self.selected_model_ids),
            "results": [dict(item) for item in self.results],
            "diagnostics": dict(self.diagnostics) if self.diagnostics else None,
        }


def qualify_catalog(
    payload: bytes | Mapping[str, Any],
    *,
    source_url: str,
    captured_at: datetime | None = None,
    freshness_seconds: int = DEFAULT_CATALOG_FRESHNESS_SECONDS,
    expected_row_count: int = DEFAULT_EXPECTED_ROW_COUNT,
    now: datetime | None = None,
) -> CatalogQualificationReport:
    """Qualify a public or management catalog without retaining raw rows."""

    try:
        snapshot = _build_snapshot(
            payload,
            source_url=source_url,
            captured_at=captured_at or datetime.now(timezone.utc),
            freshness_seconds=freshness_seconds,
            expected_row_count=expected_row_count,
        )
    except CatalogQualificationError as exc:
        return CatalogQualificationReport("unknown", None, (str(exc),))
    observed_at = now or datetime.now(timezone.utc)
    if snapshot.fresh_until <= observed_at:
        return CatalogQualificationReport("stale", snapshot)
    if snapshot.row_count != snapshot.expected_row_count or snapshot.malformed_row_count:
        return CatalogQualificationReport("partial", snapshot)
    return CatalogQualificationReport("qualified", snapshot)


def reconcile_catalog_projections(
    public: CatalogQualificationReport, management: CatalogQualificationReport
) -> CatalogProjectionReconciliation:
    """Compare projections without retaining either raw payload."""

    fields = (
        "expected_row_count",
        "row_count",
        "unique_id_count",
        "malformed_row_count",
        "duplicate_row_delta",
        "duplicate_ids",
        "provider_counts",
        "capability_counts",
        "profile_counts",
        "context_length",
        "max_output_tokens",
    )
    public_snapshot = public.snapshot
    management_snapshot = management.snapshot
    if public_snapshot is None or management_snapshot is None:
        return CatalogProjectionReconciliation(
            status="unknown",
            public_status=public.status,
            management_status=management.status,
            compared_fields=fields,
            mismatches=("one or both projections have no valid snapshot",),
        )

    mismatches: list[str] = []
    for field in fields:
        public_value = getattr(public_snapshot, field)
        management_value = getattr(management_snapshot, field)
        if field == "duplicate_ids":
            public_value = tuple(sorted(public_value, key=lambda item: str(item.get("id"))))
            management_value = tuple(sorted(management_value, key=lambda item: str(item.get("id"))))
        if public_value != management_value:
            mismatches.append(field)
    return CatalogProjectionReconciliation(
        status="consistent" if not mismatches else "contradictory",
        public_status=public.status,
        management_status=management.status,
        compared_fields=fields,
        mismatches=tuple(mismatches),
    )


def summarize_probes(
    observations: Sequence[Any],
    *,
    captured_at: datetime,
    selected_model_ids: Sequence[str] = (),
    catalog_payload_hash: str | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> ProbeQualification:
    """Convert ProbeObservation-like objects to a sanitized qualification."""

    statuses: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    results: list[dict[str, Any]] = []
    ready = 0
    for observation in observations:
        model_id = str(getattr(observation, "model_id", ""))
        status = str(getattr(observation, "status", "unknown"))
        availability = str(getattr(observation, "availability_state", "unknown"))
        statuses[status] += 1
        if availability == "ready":
            ready += 1
        error_class = getattr(observation, "error_class", None)
        if error_class:
            errors[str(error_class)] += 1
        observed_at = getattr(observation, "observed_at", captured_at)
        result = {
            "model_id": model_id,
            "availability_state": availability,
            "status": status,
            "observed_at": observed_at.isoformat()
            if isinstance(observed_at, datetime)
            else str(observed_at),
            "latency_ms": getattr(observation, "latency_ms", None),
            "http_status": getattr(observation, "http_status", None),
            "error_class": error_class,
        }
        results.append(result)
    results.sort(key=lambda item: str(item["model_id"]))
    return ProbeQualification(
        captured_at=captured_at,
        catalog_payload_hash=catalog_payload_hash,
        attempted=len(observations),
        ready=ready,
        non_ready=len(observations) - ready,
        statuses=dict(sorted(statuses.items())),
        error_classes=dict(sorted(errors.items())),
        results=tuple(results),
        selected_model_ids=tuple(selected_model_ids),
        diagnostics=dict(diagnostics) if diagnostics is not None else None,
    )


def select_probe_models(
    payload: bytes | Mapping[str, Any], *, limit: int = MAX_CATALOG_PROBE_SAMPLE
) -> tuple[str, ...]:
    """Select a deterministic, provider-diverse bounded liveness sample."""

    if limit < 1 or limit > MAX_CATALOG_PROBE_SAMPLE:
        raise CatalogQualificationError(
            f"probe sample must be between 1 and {MAX_CATALOG_PROBE_SAMPLE} models"
        )
    rows, _, _ = _extract_rows(_decode_payload(payload))
    candidates: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        model_id = str(row["id"]).strip()
        if model_id and model_id not in candidates:
            candidates[model_id] = row

    def rank(item: tuple[str, Mapping[str, Any]]) -> tuple[int, str, str]:
        model_id, row = item
        provider_name = provider(row)
        free_rank = 0 if ":free" in model_id.lower() else 1
        return free_rank, provider_name, model_id

    ordered = sorted(candidates.items(), key=rank)
    selected: list[str] = []
    providers: set[str] = set()
    for model_id, row in ordered:
        provider_name = provider(row)
        if provider_name not in providers:
            selected.append(model_id)
            providers.add(provider_name)
        if len(selected) == limit:
            return tuple(selected)
    for model_id, _ in ordered:
        if model_id not in selected:
            selected.append(model_id)
        if len(selected) == limit:
            break
    return tuple(selected)


def probe_catalog(
    payload: bytes | Mapping[str, Any],
    transport: Any,
    *,
    limit: int = MAX_CATALOG_PROBE_SAMPLE,
    timeout_seconds: float = 20.0,
    captured_at: datetime | None = None,
    live: bool = False,
    consented: bool = False,
    provider_name: str = "fixture",
) -> ProbeQualification:
    """Run an explicitly bounded, credential-safe liveness sample."""

    if timeout_seconds <= 0:
        raise CatalogQualificationError("probe timeout must be positive")
    observed_at = utc(captured_at or datetime.now(timezone.utc))
    selected = select_probe_models(payload, limit=limit)
    from verdict.probes import ProbePolicy, ProbeRunner

    runner = ProbeRunner(
        ProbePolicy(
            timeout_seconds=timeout_seconds,
            max_concurrency=min(4, max(1, len(selected))),
            max_models_per_run=limit,
        )
    )
    run = runner.run_with_diagnostics(
        selected, transport, now=observed_at, live=live, consented=consented, provider=provider_name
    )
    catalog_raw = payload if isinstance(payload, bytes) else canonical_json(payload)
    return summarize_probes(
        run.observations,
        captured_at=observed_at,
        selected_model_ids=selected,
        catalog_payload_hash=hashlib.sha256(catalog_raw).hexdigest(),
        diagnostics=run.diagnostics.to_dict(),
    )


def store_qualification(
    report: CatalogQualificationReport,
    *,
    memory_path: str | Path,
    probes: ProbeQualification | None = None,
) -> MemoryRecord:
    """Store only the sanitized catalog qualification in shared memory."""

    if report.snapshot is None:
        raise CatalogQualificationError("cannot store a catalog without a valid snapshot")
    if report.status not in {"qualified", "partial", "stale"}:
        raise CatalogQualificationError(f"cannot store catalog status {report.status}")
    snapshot = report.snapshot
    content_payload: dict[str, Any] = {"report": report.to_dict()}
    if probes:
        content_payload["probes"] = probes.to_dict()
    content = json.dumps(content_payload, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    record_id = hashlib.sha256(
        f"{snapshot.source_url}:{snapshot.payload_hash}:{CATALOG_QUALIFICATION_VERSION}:"
        f"{content_hash}".encode()
    ).hexdigest()
    record = MemoryRecord(
        record_id=record_id,
        namespace="omniroute-catalog",
        key=snapshot.source_url,
        content=content,
        source="omniroute:catalog",
        trust="runtime-observation",
        scope="shared",
        metadata={
            "qualification_version": CATALOG_QUALIFICATION_VERSION,
            "schema": snapshot.schema,
            "payload_hash": snapshot.payload_hash,
            "row_count": snapshot.row_count,
            "unique_id_count": snapshot.unique_id_count,
            "status": report.status,
            "qualification_hash": content_hash,
        },
        expires_at=snapshot.fresh_until.timestamp(),
        authority="unverified",
        authority_verified=False,
        confidence=1.0 if report.passed else 0.0,
        provenance={
            "source_url": snapshot.source_url,
            "captured_at": snapshot.captured_at.isoformat(),
            "fresh_until": snapshot.fresh_until.isoformat(),
            "payload_hash": snapshot.payload_hash,
            "schema": snapshot.schema,
            "qualification_version": CATALOG_QUALIFICATION_VERSION,
            "qualification_hash": content_hash,
        },
    )
    with MemoryPlane(memory_path) as plane:
        return plane.put(record)


def _build_snapshot(
    payload: bytes | Mapping[str, Any],
    *,
    source_url: str,
    captured_at: datetime,
    freshness_seconds: int,
    expected_row_count: int,
) -> CatalogSnapshot:
    if not source_url or not source_url.startswith(("http://", "https://")):
        raise CatalogQualificationError("source_url must be an HTTP(S) endpoint")
    if freshness_seconds <= 0 or expected_row_count <= 0:
        raise CatalogQualificationError("freshness and expected row bounds must be positive")
    raw = payload if isinstance(payload, bytes) else canonical_json(payload)
    document = _decode_payload(payload)
    rows, schema, catalog_version = _extract_rows(document)
    valid_rows: list[Mapping[str, Any]] = []
    malformed = 0
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not isinstance(row.get("id"), str)
            or not row["id"].strip()
        ):
            malformed += 1
        else:
            valid_rows.append(row)
    by_id: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in valid_rows:
        by_id[str(row["id"])].append(row)
    duplicate_ids = tuple(
        {
            "id": model_id,
            "occurrences": len(items),
            "classification": duplicate_classification(items),
        }
        for model_id, items in sorted(by_id.items())
        if len(items) > 1
    )
    return CatalogSnapshot(
        source_url=source_url,
        captured_at=utc(captured_at),
        freshness_seconds=freshness_seconds,
        schema=schema,
        payload_hash=hashlib.sha256(raw).hexdigest(),
        row_count=len(rows),
        unique_id_count=len(by_id),
        malformed_row_count=malformed,
        expected_row_count=expected_row_count,
        duplicate_ids=duplicate_ids,
        provider_counts=provider_counts(valid_rows),
        capability_counts=capability_counts(valid_rows),
        profile_counts=profile_counts(valid_rows),
        context_length=numeric_stats(valid_rows, "context_length"),
        max_output_tokens=numeric_stats(valid_rows, "max_output_tokens"),
        catalog_version=catalog_version,
    )


def _decode_payload(payload: bytes | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(payload, bytes):
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise CatalogQualificationError("catalog payload is not valid JSON") from exc
    else:
        value = payload
    if not isinstance(value, Mapping):
        raise CatalogQualificationError("catalog payload must be a JSON object")
    return value


def _extract_rows(document: Mapping[str, Any]) -> tuple[list[Any], str, str | None]:
    data = document.get("data")
    if isinstance(data, list):
        return data, "openai-model-list-v1", None
    catalog = document.get("catalog")
    version = document.get("catalogVersion")
    if isinstance(catalog, Mapping) and isinstance(version, str) and version:
        rows: list[Any] = []
        for provider, group in sorted(catalog.items(), key=lambda item: str(item[0])):
            if not isinstance(group, Mapping) or not isinstance(group.get("models"), list):
                continue
            for raw_row in group["models"]:
                if isinstance(raw_row, Mapping) and "provider" not in raw_row:
                    row = dict(raw_row)
                    row["provider"] = str(provider)
                    rows.append(row)
                else:
                    rows.append(raw_row)
        return rows, f"omniroute-management:{version}", version
    raise CatalogQualificationError(
        "catalog schema drift: expected data[] or catalog/catalogVersion"
    )


__all__ = [
    "CATALOG_QUALIFICATION_VERSION",
    "DEFAULT_CATALOG_FRESHNESS_SECONDS",
    "DEFAULT_EXPECTED_ROW_COUNT",
    "CatalogProjectionReconciliation",
    "CatalogQualificationError",
    "CatalogQualificationReport",
    "CatalogSnapshot",
    "ProbeQualification",
    "qualify_catalog",
    "reconcile_catalog_projections",
    "store_qualification",
    "summarize_probes",
]
