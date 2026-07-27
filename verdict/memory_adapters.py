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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

ADAPTER_PROTOCOL_VERSION = "1"
MANIFEST_VERSION = "1"
DEFAULT_MAX_BYTES = 1_048_576
DEFAULT_MAX_RECORDS = 10_000

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


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    protocol_version: str = ADAPTER_PROTOCOL_VERSION
    available: bool = True
    description: str = ""


@dataclass(frozen=True)
class AdapterResolution:
    adapter_id: str
    available: bool
    protocol_version: str | None
    reason: str | None = None


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
        self._adapters[adapter_id] = adapter
        self._descriptors[adapter_id] = descriptor or AdapterDescriptor(
            adapter_id=adapter_id, protocol_version=version
        )

    def declare_unavailable(self, descriptor: AdapterDescriptor) -> None:
        adapter_id = _validate_identifier(descriptor.adapter_id, "adapter_id")
        if descriptor.protocol_version != ADAPTER_PROTOCOL_VERSION:
            raise ValueError("unsupported adapter protocol version")
        self._descriptors[adapter_id] = AdapterDescriptor(
            adapter_id=adapter_id,
            protocol_version=descriptor.protocol_version,
            available=False,
            description=descriptor.description,
        )
        self._adapters.pop(adapter_id, None)

    def resolve(self, adapter_id: str) -> AdapterResolution:
        descriptor = self._descriptors.get(adapter_id)
        if descriptor is None:
            return AdapterResolution(adapter_id, False, None, "adapter is not registered")
        if not descriptor.available or adapter_id not in self._adapters:
            return AdapterResolution(
                adapter_id,
                False,
                descriptor.protocol_version,
                descriptor.description or "adapter unavailable",
            )
        return AdapterResolution(adapter_id, True, descriptor.protocol_version)

    def list(self) -> list[AdapterDescriptor]:
        return sorted(self._descriptors.values(), key=lambda descriptor: descriptor.adapter_id)

    def get(self, adapter_id: str) -> MemoryAdapter:
        resolution = self.resolve(adapter_id)
        if not resolution.available:
            raise AdapterUnavailableError(adapter_id, resolution.reason or "adapter unavailable")
        return self._adapters[adapter_id]


class AdapterUnavailableError(RuntimeError):
    """Raised when a declared integration cannot be used in this installation."""

    def __init__(self, adapter_id: str, reason: str) -> None:
        super().__init__(f"adapter '{adapter_id}' unavailable: {reason}")
        self.adapter_id = adapter_id
        self.reason = reason


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
    )


def content_hash(record: Mapping[str, Any]) -> str:
    """Return a stable hash over canonical, redacted record content."""
    canonical = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


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
    "AdapterRegistry",
    "AdapterResolution",
    "AdapterUnavailableError",
    "ImportPolicy",
    "LocalManifestAdapter",
    "ManifestError",
    "ManifestReport",
    "MemoryAdapter",
    "content_hash",
    "export_manifest",
    "import_manifest",
]
