"""Safe, provider-neutral memory adapter and manifest boundaries.

This module deliberately handles manifests, not private database formats.  An
adapter may be registered by an application, but imports remain explicit,
allowlisted, bounded, and redacted before content reaches a caller or store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

ADAPTER_PROTOCOL_VERSION = "1"
MANIFEST_VERSION = "1"
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_RECORDS = 10_000
AdapterStatus = Literal["available", "degraded", "unavailable", "unknown"]

_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|password|passwd|token|credential|authorization|cookie)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|basic\s+|sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|xox[baprs]-[a-z0-9-]{8,})"
)
_PROMPT_KEY = re.compile(
    r"(?i)(?:^|[_-])(prompt|completion|messages|conversation|transcript)(?:$|[_-])"
)


class MemoryAdapter(Protocol):
    """Versioned adapter contract; implementations must be explicitly available."""

    adapter_id: str
    protocol_version: str

    def export(self, *, root: Path, options: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]: ...

    def import_records(
        self, records: Iterable[Mapping[str, Any]], *, options: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class LocalManifestAdapter:
    """Adapter for the canonical manifest boundary.

    This adapter intentionally knows only the versioned manifest format.  It
    does not inspect or import any provider-specific database, so optional
    integrations cannot become hidden storage authorities.
    """

    adapter_id = "local-manifest"
    protocol_version = ADAPTER_PROTOCOL_VERSION

    def export(self, *, root: Path, options: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        records = options.get("records", ())
        if not isinstance(records, Iterable) or isinstance(records, (str, bytes, Mapping)):
            raise ValueError("records must be an iterable of mappings")
        return cast(Iterable[Mapping[str, Any]], records)

    def import_records(
        self, records: Iterable[Mapping[str, Any]], *, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {"records": sum(1 for _ in records), "status": "accepted"}


class CanonicalManifestAdapter(LocalManifestAdapter):
    """Provider-named adapter restricted to the canonical manifest boundary.

    The name is useful to callers that want to report MasterDocsRAG or Code
    Review Graph capability without granting either integration permission to
    read a private database.  Only caller-supplied, versioned manifest records
    cross this boundary.
    """

    def __init__(self, adapter_id: str, *, source: str, formats: tuple[str, ...]):
        self.adapter_id = _validate_identifier(adapter_id, "adapter_id")
        self.source = _safe_label(source)
        self.formats = formats

    def export(self, *, root: Path, options: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        records = super().export(root=root, options=options)
        return tuple(
            _with_boundary_metadata(record, source=self.source, adapter_id=self.adapter_id)
            for record in records
        )


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    protocol_version: str = ADAPTER_PROTOCOL_VERSION
    available: bool = True
    description: str = ""
    status: AdapterStatus | None = None
    formats: tuple[str, ...] = ()
    boundary: str = "canonical-manifest"

    @property
    def effective_status(self) -> AdapterStatus:
        if self.status is not None:
            return self.status
        return "available" if self.available else "unavailable"


@dataclass(frozen=True)
class AdapterResolution:
    adapter_id: str
    available: bool
    protocol_version: str | None
    reason: str | None = None
    status: AdapterStatus = "unknown"


class AdapterRegistry:
    """Registry that reports unavailable integrations instead of importing them."""

    def __init__(self) -> None:
        self._adapters: dict[str, MemoryAdapter] = {}
        self._descriptors: dict[str, AdapterDescriptor] = {}

    def register(
        self, adapter: MemoryAdapter, *, descriptor: AdapterDescriptor | None = None
    ) -> None:
        adapter_id = _validate_identifier(adapter.adapter_id, "adapter_id")
        version = _validate_identifier(adapter.protocol_version, "protocol_version")
        if version != ADAPTER_PROTOCOL_VERSION:
            raise ValueError(f"unsupported adapter protocol version: {version}")
        chosen = descriptor or AdapterDescriptor(adapter_id=adapter_id, protocol_version=version)
        if chosen.adapter_id != adapter_id:
            raise ValueError("adapter descriptor id does not match implementation")
        if chosen.protocol_version != version:
            raise ValueError("adapter descriptor version does not match implementation")
        if chosen.effective_status == "unavailable":
            self.declare_unavailable(chosen)
            return
        self._adapters[adapter_id] = adapter
        self._descriptors[adapter_id] = chosen

    def declare_unavailable(self, descriptor: AdapterDescriptor) -> None:
        adapter_id = _validate_identifier(descriptor.adapter_id, "adapter_id")
        if descriptor.protocol_version != ADAPTER_PROTOCOL_VERSION:
            raise ValueError("unsupported adapter protocol version")
        self._descriptors[adapter_id] = AdapterDescriptor(
            adapter_id=adapter_id,
            protocol_version=descriptor.protocol_version,
            available=False,
            description=descriptor.description,
            status="unavailable",
            formats=descriptor.formats,
            boundary=descriptor.boundary,
        )
        self._adapters.pop(adapter_id, None)

    def resolve(self, adapter_id: str) -> AdapterResolution:
        descriptor = self._descriptors.get(adapter_id)
        if descriptor is None:
            return AdapterResolution(
                adapter_id, False, None, "adapter is not registered", "unknown"
            )
        status = descriptor.effective_status
        if status == "unavailable" or adapter_id not in self._adapters:
            return AdapterResolution(
                adapter_id,
                False,
                descriptor.protocol_version,
                descriptor.description or "adapter unavailable",
                status,
            )
        return AdapterResolution(
            adapter_id,
            status in {"available", "degraded"},
            descriptor.protocol_version,
            descriptor.description if status != "available" else None,
            status,
        )

    def list(self) -> list[AdapterDescriptor]:
        return sorted(self._descriptors.values(), key=lambda descriptor: descriptor.adapter_id)

    def get(self, adapter_id: str) -> MemoryAdapter:
        resolution = self.resolve(adapter_id)
        if not resolution.available:
            raise AdapterUnavailableError(adapter_id, resolution.reason or "adapter unavailable")
        return self._adapters[adapter_id]

    def status(self) -> Sequence[dict[str, Any]]:
        """Return non-sensitive capability and health metadata for every adapter."""
        return [
            {
                "adapter_id": descriptor.adapter_id,
                "protocol_version": descriptor.protocol_version,
                "status": descriptor.effective_status,
                "available": descriptor.effective_status in {"available", "degraded"},
                "description": descriptor.description,
                "formats": list(descriptor.formats),
                "boundary": descriptor.boundary,
            }
            for descriptor in self.list()
        ]

    def ingest(
        self, adapter_id: str, *, plane: Any, root: Path, options: Mapping[str, Any] | None = None
    ) -> AdapterIngestionReport:
        """Run one adapter while isolating adapter and record failures."""
        resolution = self.resolve(adapter_id)
        if not resolution.available:
            return AdapterIngestionReport(
                adapter_id=adapter_id,
                status="unavailable" if resolution.status == "unavailable" else "unknown",
                errors=(resolution.reason or "adapter unavailable",),
            )
        adapter = self.get(adapter_id)
        opts = dict(options or {})
        try:
            exported = adapter.export(root=root, options=opts)
            records = list(exported)
        except Exception as exc:  # adapter failures must not abort the aggregate run
            return AdapterIngestionReport(
                adapter_id=adapter_id, status="degraded", errors=(_safe_error(exc),)
            )

        accepted = rejected = duplicates = superseded = 0
        errors: list[str] = []
        converter = getattr(adapter, "to_memory_record", None)
        for raw in records:
            try:
                record = converter(raw) if callable(converter) else _memory_record_from_mapping(raw)
                provenance = dict(record.provenance or {})
                provenance.setdefault("adapter_protocol_version", ADAPTER_PROTOCOL_VERSION)
                provenance.setdefault("schema_version", getattr(record, "schema_version", 2))
                record = replace(record, provenance=provenance)
                before = plane.get(record.namespace, record.key, scope=record.scope)
                stored = plane.put(record)
                if before is not None and before.content_hash == record.content_hash:
                    duplicates += 1
                else:
                    accepted += 1
                    superseded += int(stored.supersedes is not None)
            except Exception as exc:  # one malformed record must not discard its batch
                rejected += 1
                errors.append(_safe_error(exc))
        adapter_report: Any = getattr(adapter, "last_report", None)
        adapter_errors = list(getattr(adapter_report, "errors", ()))
        adapter_status = str(getattr(adapter_report, "status", "ok"))
        if adapter_status in {"partial", "rejected", "quarantined"}:
            adapter_errors.append(f"source reported {adapter_status}")
        status: AdapterStatus = (
            "degraded"
            if rejected or adapter_errors or resolution.status == "degraded"
            else "available"
        )
        return AdapterIngestionReport(
            adapter_id=adapter_id,
            status=status,
            records_seen=len(records),
            records_accepted=accepted,
            records_rejected=rejected,
            duplicates=duplicates,
            superseded=superseded,
            errors=tuple(errors + adapter_errors),
        )

    def ingest_many(
        self, requests: Iterable[Mapping[str, Any]], *, plane: Any, root: Path
    ) -> AdapterIngestionSummary:
        """Run independent adapter requests and aggregate their reports."""
        reports: list[AdapterIngestionReport] = []
        for request in requests:
            adapter_id = request.get("adapter_id")
            if not isinstance(adapter_id, str):
                reports.append(
                    AdapterIngestionReport(
                        adapter_id="unknown",
                        status="unknown",
                        errors=("request is missing adapter_id",),
                    )
                )
                continue
            reports.append(
                self.ingest(
                    adapter_id,
                    plane=plane,
                    root=root,
                    options=request.get("options")
                    if isinstance(request.get("options"), Mapping)
                    else {},
                )
            )
        status: AdapterStatus = (
            "degraded"
            if any(report.status == "degraded" for report in reports)
            else "unknown"
            if any(report.status == "unknown" for report in reports)
            else "unavailable"
            if reports and all(report.status == "unavailable" for report in reports)
            else "available"
        )
        return AdapterIngestionSummary(status=status, reports=tuple(reports))


class AdapterUnavailableError(RuntimeError):
    """Raised when a declared integration cannot be used in this installation."""

    def __init__(self, adapter_id: str, reason: str) -> None:
        super().__init__(f"adapter '{adapter_id}' unavailable: {reason}")
        self.adapter_id = adapter_id
        self.reason = reason


@dataclass(frozen=True)
class AdapterIngestionReport:
    """Bounded, non-sensitive result for one registry ingestion."""

    adapter_id: str
    status: AdapterStatus
    records_seen: int = 0
    records_accepted: int = 0
    records_rejected: int = 0
    duplicates: int = 0
    superseded: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "status": self.status,
            "records_seen": self.records_seen,
            "records_accepted": self.records_accepted,
            "records_rejected": self.records_rejected,
            "duplicates": self.duplicates,
            "superseded": self.superseded,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class AdapterIngestionSummary:
    status: AdapterStatus
    reports: tuple[AdapterIngestionReport, ...]

    @property
    def records_accepted(self) -> int:
        return sum(report.records_accepted for report in self.reports)

    @property
    def records_rejected(self) -> int:
        return sum(report.records_rejected for report in self.reports)

    @property
    def duplicates(self) -> int:
        return sum(report.duplicates for report in self.reports)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "records_accepted": self.records_accepted,
            "records_rejected": self.records_rejected,
            "duplicates": self.duplicates,
            "reports": [report.to_dict() for report in self.reports],
        }


class DocumentMemoryAdapter:
    """Expose the deterministic document adapter through the registry."""

    adapter_id = "document"
    protocol_version = ADAPTER_PROTOCOL_VERSION

    def __init__(self) -> None:
        self.last_report: Any = None

    def export(self, *, root: Path, options: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        from verdict.memory_document_adapter import DocumentIngestionPolicy, DocumentIngestor

        raw_paths = options.get("paths", (root,))
        if isinstance(raw_paths, (str, bytes, Path)):
            raw_paths = (raw_paths,)
        if not isinstance(raw_paths, Iterable):
            raise ValueError("document adapter paths must be iterable")
        policy = DocumentIngestionPolicy(
            (root,),
            max_file_bytes=int(options.get("max_file_bytes", 1_048_576)),
            chunk_size=int(options.get("chunk_size", 1_200)),
        )
        result = DocumentIngestor(policy).ingest(
            raw_paths,
            namespace=str(options.get("namespace", "documents")),
            scope=str(options.get("scope", "default")),
            source=str(options.get("source", "document")),
            dry_run=bool(options.get("dry_run", False)),
        )
        self.last_report = result.report
        return result.records

    def import_records(
        self, records: Iterable[Mapping[str, Any]], *, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {"records": sum(1 for _ in records), "status": "accepted"}


class SessionMemoryAdapter:
    """Expose the explicit JSONL session adapter through the registry."""

    adapter_id = "session-jsonl"
    protocol_version = ADAPTER_PROTOCOL_VERSION

    def __init__(self) -> None:
        self.last_report: Any = None

    def export(self, *, root: Path, options: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        from verdict.memory_session_adapter import SessionAdapter

        source = options.get("source")
        if not isinstance(source, (str, Path)):
            raise ValueError("session adapter requires an explicit source path")
        result = SessionAdapter().import_file(
            source,
            project=str(options.get("project", "default")),
            session_id=str(options.get("session_id", Path(source).stem)),
            format=str(options.get("format", "jsonl")),
        )
        self.last_report = result.report
        if result.report.status in {"unavailable", "error"}:
            raise ValueError(
                result.report.errors[0] if result.report.errors else "session import failed"
            )
        return result.records

    def import_records(
        self, records: Iterable[Mapping[str, Any]], *, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {"records": sum(1 for _ in records), "status": "accepted"}


class ManifestRecordsAdapter(LocalManifestAdapter):
    """Adapter for a provider's exported canonical records, never its private DB."""

    def __init__(self, adapter_id: str, source: str):
        self.adapter_id = _validate_identifier(adapter_id, "adapter_id")
        self.source = _safe_label(source)

    def export(self, *, root: Path, options: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
        records = super().export(root=root, options=options)
        return tuple(
            _with_boundary_metadata(record, source=self.source, adapter_id=self.adapter_id)
            for record in records
        )


def build_default_adapter_registry() -> AdapterRegistry:
    """Build the local-first registry without importing provider SDKs or DBs."""
    registry = AdapterRegistry()
    registry.register(
        LocalManifestAdapter(),
        descriptor=AdapterDescriptor(
            "local-manifest", description="canonical versioned manifest", formats=("json",)
        ),
    )
    registry.register(
        DocumentMemoryAdapter(),
        descriptor=AdapterDescriptor(
            "document",
            description="allowlisted Markdown/text documents",
            formats=("md", "markdown", "txt"),
        ),
    )
    registry.register(
        SessionMemoryAdapter(),
        descriptor=AdapterDescriptor(
            "session-jsonl",
            description="explicit provider-neutral/provider-exported JSONL",
            formats=("jsonl", "claude-jsonl", "codex-jsonl", "pi-jsonl"),
        ),
    )
    registry.register(
        ManifestRecordsAdapter("masterdocs-manifest", "masterdocs-export"),
        descriptor=AdapterDescriptor(
            "masterdocs-manifest", description="MasterDocs exported manifest", formats=("json",)
        ),
    )
    registry.register(
        ManifestRecordsAdapter("code-graph-manifest", "code-review-graph-export"),
        descriptor=AdapterDescriptor(
            "code-graph-manifest",
            description="Code Review Graph exported manifest",
            formats=("json",),
        ),
    )
    unavailable = (
        ("codex-session", "provider-specific Codex state requires an explicit JSONL export"),
        ("claude-session", "provider-specific Claude state requires an explicit JSONL export"),
        ("pi-session", "provider-specific Pi state requires an explicit JSONL export"),
        ("ruflo-session", "provider-specific Ruflo state requires an explicit JSONL export"),
        ("masterdocs-sqlite", "private database boundary unsupported; use masterdocs-manifest"),
        ("code-graph-sqlite", "private database boundary unsupported; use code-graph-manifest"),
    )
    for adapter_id, reason in unavailable:
        registry.declare_unavailable(
            AdapterDescriptor(
                adapter_id,
                available=False,
                status="unavailable",
                description=reason,
                formats=("jsonl",) if adapter_id.endswith("session") else ("sqlite",),
            )
        )
    return registry


def _with_boundary_metadata(
    record: Mapping[str, Any], *, source: str, adapter_id: str
) -> dict[str, Any]:
    item = dict(record)
    provenance = dict(item.get("provenance") or {})
    provenance.setdefault("source", source)
    provenance.setdefault("adapter_id", adapter_id)
    provenance.setdefault("adapter_protocol_version", ADAPTER_PROTOCOL_VERSION)
    provenance.setdefault("schema_version", MANIFEST_VERSION)
    item["provenance"] = provenance
    return item


def _memory_record_from_mapping(raw: Mapping[str, Any]) -> Any:
    from dataclasses import fields

    from verdict.memory_plane import MemoryRecord

    allowed = {field.name for field in fields(MemoryRecord)}
    item = {key: value for key, value in raw.items() if key in allowed}
    return MemoryRecord(**item)


def _safe_error(error: Exception) -> str:
    text = str(error).replace("\n", " ").strip()
    return text[:256] or error.__class__.__name__


@dataclass(frozen=True)
class ImportPolicy:
    """Filesystem and content limits for manifest import/export."""

    allowed_roots: tuple[Path, ...]
    max_bytes: int = DEFAULT_MAX_BYTES
    max_records: int = DEFAULT_MAX_RECORDS
    allow_symlinks: bool = False
    redact: bool = True

    def __post_init__(self) -> None:
        roots = tuple(path.expanduser().resolve() for path in self.allowed_roots)
        if not roots:
            raise ValueError("at least one allowlisted root is required")
        if self.max_bytes <= 0 or self.max_records <= 0:
            raise ValueError("limits must be positive")
        object.__setattr__(self, "allowed_roots", roots)


@dataclass(frozen=True)
class ManifestReport:
    """Stable machine-readable result for export/import operations."""

    operation: str
    manifest_version: str = MANIFEST_VERSION
    dry_run: bool = False
    status: str = "ok"
    records_seen: int = 0
    records_written: int = 0
    duplicates: int = 0
    skipped: int = 0
    redacted: int = 0
    unavailable_adapters: tuple[str, ...] = ()
    manifest_hash: str | None = None
    manifest_hash_verified: bool = False
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "manifest_version": self.manifest_version,
            "dry_run": self.dry_run,
            "status": self.status,
            "records_seen": self.records_seen,
            "records_written": self.records_written,
            "duplicates": self.duplicates,
            "skipped": self.skipped,
            "redacted": self.redacted,
            "unavailable_adapters": list(self.unavailable_adapters),
            "manifest_hash": self.manifest_hash,
            "manifest_hash_verified": self.manifest_hash_verified,
            "errors": list(self.errors),
        }


class ManifestError(ValueError):
    """Raised when a manifest violates its version or safety policy."""


def export_manifest(
    records: Iterable[Mapping[str, Any]],
    destination: str | os.PathLike[str],
    *,
    policy: ImportPolicy,
    source: str = "local",
    adapter_id: str = "local",
    dry_run: bool = False,
) -> ManifestReport:
    """Write a redacted JSON manifest atomically, or report what would happen."""
    path = _safe_path(destination, policy, for_write=True)
    prepared: list[dict[str, Any]] = []
    redacted_count = 0
    seen_hashes: set[str] = set()
    errors: list[str] = []
    for index, record in enumerate(records):
        if index >= policy.max_records:
            errors.append("record limit exceeded")
            break
        item, changed = _prepare_record(
            record, source=source, adapter_id=adapter_id, redact=policy.redact
        )
        redacted_count += int(changed)
        digest = content_hash(item)
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        item["content_hash"] = digest
        prepared.append(item)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "adapter_protocol_version": ADAPTER_PROTOCOL_VERSION,
        "source": _safe_label(source),
        "adapter_id": _safe_label(adapter_id),
        "records": prepared,
    }
    manifest["manifest_hash"] = _manifest_digest(manifest)
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    if len(encoded) > policy.max_bytes:
        errors.append("manifest size limit exceeded")
    if not dry_run and not errors:
        _atomic_write(path, encoded)
    return ManifestReport(
        operation="export",
        dry_run=dry_run,
        status="error" if errors else "ok",
        records_seen=len(prepared),
        records_written=0 if dry_run or errors else len(prepared),
        duplicates=0,
        redacted=redacted_count,
        manifest_hash=str(manifest["manifest_hash"]),
        manifest_hash_verified=True,
        errors=tuple(errors),
    )


def import_manifest(
    source: str | os.PathLike[str],
    *,
    policy: ImportPolicy,
    existing_hashes: Iterable[str] = (),
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], ManifestReport]:
    """Read and validate a manifest; return deduplicated safe records and a report."""
    path = _safe_path(source, policy, for_write=False)
    try:
        size = path.stat().st_size
        if size > policy.max_bytes:
            raise ManifestError("manifest size limit exceeded")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest safely: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("manifest_version") != MANIFEST_VERSION:
        raise ManifestError("unsupported manifest version")
    if payload.get("adapter_protocol_version") != ADAPTER_PROTOCOL_VERSION:
        raise ManifestError("unsupported adapter protocol version")
    supplied_manifest_hash = payload.get("manifest_hash")
    if not isinstance(supplied_manifest_hash, str):
        raise ManifestError("manifest hash is missing")
    if supplied_manifest_hash != _manifest_digest(payload):
        raise ManifestError("manifest hash mismatch")
    raw_records = payload.get("records")
    if not isinstance(raw_records, list) or len(raw_records) > policy.max_records:
        raise ManifestError("invalid or oversized records list")
    accepted: list[dict[str, Any]] = []
    known = set(existing_hashes)
    seen: set[str] = set()
    duplicates = 0
    redacted_count = 0
    for record in raw_records:
        if not isinstance(record, dict):
            raise ManifestError("each record must be an object")
        item, changed = _prepare_record(
            record,
            source=_record_source(record, payload),
            adapter_id=_payload_label(payload, "adapter_id", "unknown"),
            redact=policy.redact,
        )
        digest = content_hash(item)
        supplied = record.get("content_hash")
        if supplied is not None and supplied != digest:
            raise ManifestError("content hash mismatch")
        if digest in known or digest in seen:
            duplicates += 1
            continue
        item["content_hash"] = digest
        accepted.append(item)
        seen.add(digest)
        redacted_count += int(changed)
    return accepted, ManifestReport(
        operation="import",
        dry_run=dry_run,
        records_seen=len(raw_records),
        records_written=0 if dry_run else len(accepted),
        duplicates=duplicates,
        redacted=redacted_count,
        manifest_hash=supplied_manifest_hash,
        manifest_hash_verified=True,
    )


def content_hash(record: Mapping[str, Any]) -> str:
    """Return a stable hash over canonical, redacted record content."""
    canonical = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _manifest_digest(manifest: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _prepare_record(
    record: Mapping[str, Any], *, source: str, adapter_id: str, redact: bool
) -> tuple[dict[str, Any], bool]:
    if not isinstance(record, Mapping):
        raise ManifestError("record must be an object")
    item = _redact(dict(record)) if redact else dict(record)
    item.pop("content_hash", None)
    provenance = item.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance.setdefault("source", _safe_label(source))
    provenance.setdefault("adapter_id", _safe_label(adapter_id))
    provenance.setdefault("schema_version", MANIFEST_VERSION)
    item["provenance"] = provenance
    return item, item != dict(record)


def _record_source(record: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    provenance = record.get("provenance")
    if isinstance(provenance, Mapping) and isinstance(provenance.get("source"), str):
        return str(provenance["source"])
    return _payload_label(payload, "source", "unknown")


def _payload_label(payload: Mapping[str, Any], key: str, default: str) -> str:
    value = payload.get(key, default)
    return value if isinstance(value, str) else default


def _redact(value: Any, *, key: str = "") -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if _SECRET_KEY.search(name) or _PROMPT_KEY.search(name):
                result[name] = "[redacted]"
            else:
                result[name] = _redact(raw_value, key=name)
        return result
    if isinstance(value, list):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str) and (_SECRET_VALUE.search(value) or _SECRET_KEY.search(key)):
        return "[redacted]"
    return value


def _safe_path(value: str | os.PathLike[str], policy: ImportPolicy, *, for_write: bool) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise ManifestError("manifest path must be absolute")
    if candidate_is_tmp_root(raw) or Path(tempfile.gettempdir()) in policy.allowed_roots:
        raise ManifestError(f"{tempfile.gettempdir()} is not an allowed manifest root")
    if raw.exists() and raw.is_symlink() and not policy.allow_symlinks:
        raise ManifestError("symlink paths are not allowed")
    candidate = raw.resolve(strict=False)
    if not any(candidate == root or root in candidate.parents for root in policy.allowed_roots):
        raise ManifestError("path is outside the allowlisted roots")
    if for_write:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if (
            candidate.exists()
            and stat.S_ISLNK(candidate.stat().st_mode)
            and not policy.allow_symlinks
        ):
            raise ManifestError("symlink destination is not allowed")
    return candidate


def _atomic_write(path: Path, data: bytes) -> None:
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_label(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value) else "redacted"


def candidate_is_tmp_root(path: Path) -> bool:
    """Reject the conventional shared root, not explicitly allowlisted children."""
    return path == Path(tempfile.gettempdir())


def _validate_identifier(value: str, field_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
        raise ValueError(f"invalid {field_name}")
    return value


__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "MANIFEST_VERSION",
    "AdapterDescriptor",
    "AdapterIngestionReport",
    "AdapterIngestionSummary",
    "AdapterRegistry",
    "AdapterResolution",
    "AdapterStatus",
    "AdapterUnavailableError",
    "CanonicalManifestAdapter",
    "DocumentMemoryAdapter",
    "ImportPolicy",
    "LocalManifestAdapter",
    "ManifestError",
    "ManifestRecordsAdapter",
    "ManifestReport",
    "MemoryAdapter",
    "SessionMemoryAdapter",
    "build_default_adapter_registry",
    "content_hash",
    "export_manifest",
    "import_manifest",
]
