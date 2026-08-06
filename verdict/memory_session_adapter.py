"""Provider-neutral, privacy-safe session JSONL import.

The session adapter is deliberately a file-format boundary.  It accepts either
an explicit JSONL file supplied by the caller or regular JSONL files discovered
from known local session locations.  Discovery is read-only, bounded, and never
opens provider databases or invokes a provider SDK.  Imported records are
normalized into stable, redacted dictionaries suitable for a ``MemoryPlane`` or
a manifest export.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from verdict.memory_plane import MemoryRecord

SESSION_ADAPTER_PROTOCOL_VERSION = "1"
SESSION_SCHEMA_VERSION = "1"
SESSION_FORMAT = "jsonl"
SESSION_FORMAT_VERSION = 1
SessionImportStatus = Literal["ok", "partial", "error", "unavailable"]


@dataclass(frozen=True)
class SessionFormatDescriptor:
    """Explicit provider format capability without importing provider state."""

    format_id: str
    provider: str
    description: str
    schema_versions: tuple[str, ...] = (SESSION_SCHEMA_VERSION,)
    available: bool = True


DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_LINE_BYTES = 256 * 1024
DEFAULT_MAX_RECORDS = 10_000
DEFAULT_MAX_DISCOVERY_FILES = 64

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SECRET_KEY = re.compile(
    r"(?:api[_-]?key|access[_-]?key|secret|password|passwd|token|credential|"
    r"authorization|cookie|private[_-]?key)",
    re.IGNORECASE,
)
_PROMPT_KEY = re.compile(
    r"(?:^|[_-])(prompt|input|messages|conversation|transcript|context)(?:$|[_-])", re.IGNORECASE
)
_SECRET_VALUE = re.compile(
    r"(?i)(?:bearer\s+|basic\s+|sk-[a-z0-9_-]{8,}|gh[pousr]_[a-z0-9_]{8,}|"
    r"xox[baprs]-[a-z0-9-]{8,}|eyJ[a-z0-9_-]{16,}\.[a-z0-9_-]{8,})"
)
_ROLE_ALIASES = {
    "human": "user",
    "ai": "assistant",
    "bot": "assistant",
    "model": "assistant",
    "function": "tool",
}
_TOOL_EVENTS = {"tool", "tool_call", "tool_result", "function_call", "function_result"}

_KNOWN_SESSION_GLOBS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "claude",
        "claude-jsonl",
        (
            ".claude/projects/**/*.jsonl",
            ".claude/sessions/**/*.jsonl",
        ),
    ),
    (
        "codex",
        "codex-jsonl",
        (
            ".codex/sessions/**/*.jsonl",
            ".codex/projects/**/*.jsonl",
        ),
    ),
    (
        "pi",
        "pi-jsonl",
        (
            ".pi/sessions/**/*.jsonl",
            ".pi/conversations/**/*.jsonl",
            ".pi-subagents/artifacts/*transcript.jsonl",
        ),
    ),
    (
        "ruflo",
        "ruflo-jsonl",
        (
            ".ruflo/sessions/**/*.jsonl",
            ".claude-flow/sessions/**/*.jsonl",
        ),
    ),
)


@dataclass(frozen=True)
class SessionImportPolicy:
    """Bounds and privacy defaults for one explicit session import."""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    max_records: int = DEFAULT_MAX_RECORDS
    include_raw_prompts: bool = False
    redact_credentials: bool = True
    supported_versions: tuple[str, ...] = (SESSION_SCHEMA_VERSION,)

    def __post_init__(self) -> None:
        if self.max_file_bytes <= 0 or self.max_line_bytes <= 0 or self.max_records <= 0:
            raise ValueError("session import limits must be positive")
        versions = tuple(str(version) for version in self.supported_versions)
        if not versions:
            raise ValueError("at least one session schema version is required")
        object.__setattr__(self, "supported_versions", versions)


@dataclass(frozen=True)
class SessionDiscoveryPolicy:
    """Bounds for read-only automatic session JSONL discovery."""

    roots: tuple[str | Path, ...] = ()
    providers: tuple[str, ...] = ("claude", "codex", "pi", "ruflo")
    max_files: int = DEFAULT_MAX_DISCOVERY_FILES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    def __post_init__(self) -> None:
        if self.max_files <= 0 or self.max_file_bytes <= 0:
            raise ValueError("session discovery limits must be positive")
        providers = tuple(provider.lower() for provider in self.providers)
        known = {provider for provider, _, _ in _KNOWN_SESSION_GLOBS}
        if not providers or any(provider not in known for provider in providers):
            raise ValueError("unsupported session discovery provider")
        object.__setattr__(self, "providers", providers)
        object.__setattr__(self, "roots", tuple(Path(root) for root in self.roots))


@dataclass(frozen=True)
class SessionCandidate:
    """One regular JSONL file discovered from a known provider-neutral location."""

    provider: str
    format: str
    path: Path
    session_id: str
    modified_at: float
    size_bytes: int
    file_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "format": self.format,
            "path": str(self.path),
            "session_id": self.session_id,
            "modified_at": self.modified_at,
            "size_bytes": self.size_bytes,
            "file_sha256": self.file_sha256,
        }


@dataclass(frozen=True)
class SessionImportReport:
    """Stable outcome metadata; error text never includes record contents."""

    operation: str = "session-import"
    adapter_protocol_version: str = SESSION_ADAPTER_PROTOCOL_VERSION
    format: str = "jsonl"
    status: SessionImportStatus = "ok"
    records_seen: int = 0
    records_emitted: int = 0
    malformed_records: int = 0
    skipped_records: int = 0
    redacted_fields: int = 0
    errors: tuple[str, ...] = ()

    @property
    def records_accepted(self) -> int:
        """Compatibility name for callers using the generic adapter contract."""
        return self.records_emitted

    @property
    def skipped(self) -> int:
        """Compatibility name for callers using the generic adapter contract."""
        return self.skipped_records

    @property
    def redacted(self) -> int:
        """Compatibility name for callers using the generic adapter contract."""
        return self.redacted_fields

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "adapter_protocol_version": self.adapter_protocol_version,
            "format": self.format,
            "status": self.status,
            "records_seen": self.records_seen,
            "records_emitted": self.records_emitted,
            "malformed_records": self.malformed_records,
            "skipped_records": self.skipped_records,
            "redacted_fields": self.redacted_fields,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class SessionDiscoveryReport:
    """Secret-free report for automatic discovery."""

    operation: str = "session-discovery"
    adapter_protocol_version: str = SESSION_ADAPTER_PROTOCOL_VERSION
    status: SessionImportStatus = "ok"
    roots_scanned: int = 0
    candidates_found: int = 0
    skipped_files: int = 0
    latest_modified_at: float | None = None
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "adapter_protocol_version": self.adapter_protocol_version,
            "status": self.status,
            "roots_scanned": self.roots_scanned,
            "candidates_found": self.candidates_found,
            "skipped_files": self.skipped_files,
            "latest_modified_at": self.latest_modified_at,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class SessionDiscoveryResult:
    """Discovered candidates plus bounded discovery metadata."""

    candidates: tuple[SessionCandidate, ...]
    report: SessionDiscoveryReport


@dataclass(frozen=True)
class SessionAutoImportReport:
    """Secret-free report for discovery/import orchestration."""

    operation: str = "session-auto-import"
    adapter_protocol_version: str = SESSION_ADAPTER_PROTOCOL_VERSION
    status: SessionImportStatus = "ok"
    dry_run: bool = True
    candidates_found: int = 0
    files_imported: int = 0
    duplicate_files: int = 0
    records_emitted: int = 0
    malformed_records: int = 0
    redacted_fields: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "adapter_protocol_version": self.adapter_protocol_version,
            "status": self.status,
            "dry_run": self.dry_run,
            "candidates_found": self.candidates_found,
            "files_imported": self.files_imported,
            "duplicate_files": self.duplicate_files,
            "records_emitted": self.records_emitted,
            "malformed_records": self.malformed_records,
            "redacted_fields": self.redacted_fields,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class SessionAutoImportResult:
    """Result of a bounded automatic discovery/import pass."""

    candidates: tuple[SessionCandidate, ...]
    records: tuple[dict[str, Any], ...]
    manifests: tuple[dict[str, Any], ...]
    report: SessionAutoImportReport


@dataclass(frozen=True)
class SessionImportResult:
    """Normalized records and a deterministic, serializable manifest."""

    records: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]
    report: SessionImportReport

    @property
    def manifest_json(self) -> str:
        return _canonical_json(self.manifest)

    @property
    def manifest_hash(self) -> str:
        return _manifest_hash(self.manifest)

    @property
    def memory_records(self) -> tuple[MemoryRecord, ...]:
        """Return records converted to the durable MemoryPlane contract."""
        return tuple(session_record_to_memory_record(record) for record in self.records)


class SessionAdapter:
    """Import caller-supplied JSONL without provider or private-state access."""

    adapter_id = "session-jsonl"
    protocol_version = SESSION_ADAPTER_PROTOCOL_VERSION
    format_descriptors = (
        SessionFormatDescriptor("jsonl", "provider-neutral", "canonical JSONL session events"),
        SessionFormatDescriptor("claude-jsonl", "claude", "Claude Code exported JSONL"),
        SessionFormatDescriptor("codex-jsonl", "codex", "Codex exported JSONL"),
        SessionFormatDescriptor("pi-jsonl", "pi", "Pi exported JSONL"),
        SessionFormatDescriptor("ruflo-jsonl", "ruflo", "Ruflo exported JSONL"),
    )

    def import_file(
        self,
        source: str | Path,
        *,
        project: str,
        session_id: str,
        format: str | None = None,
        policy: SessionImportPolicy | None = None,
    ) -> SessionImportResult:
        """Import one explicit JSONL path, returning unavailable for other formats."""
        selected_format = _select_format(source, format)
        supported_formats = {descriptor.format_id for descriptor in self.format_descriptors}
        if selected_format not in supported_formats:
            return _unavailable(selected_format, f"unsupported session format: {selected_format}")
        try:
            project_id = _validate_identifier(project, "project")
            session_key = _validate_identifier(session_id, "session_id")
        except ValueError as exc:
            return _unavailable("jsonl", str(exc), status="error")
        return self._import_jsonl(
            Path(source),
            project=project_id,
            session_id=session_key,
            format=selected_format,
            policy=policy or SessionImportPolicy(),
        )

    def import_session(
        self,
        source: str | Path,
        *,
        project: str,
        session_id: str,
        format: str | None = None,
        policy: SessionImportPolicy | None = None,
    ) -> SessionImportResult:
        """Alias for the explicit file import operation."""
        return self.import_file(
            source, project=project, session_id=session_id, format=format, policy=policy
        )

    def discover_sessions(
        self, policy: SessionDiscoveryPolicy | None = None
    ) -> SessionDiscoveryResult:
        """Discover latest modified regular JSONL session files from known locations."""
        discovery_policy = policy or SessionDiscoveryPolicy()
        roots = _discovery_roots(discovery_policy)
        candidates: list[SessionCandidate] = []
        errors: list[str] = []
        skipped = 0
        seen_paths: set[Path] = set()

        for root in roots:
            for provider, format_id, patterns in _KNOWN_SESSION_GLOBS:
                if provider not in discovery_policy.providers:
                    continue
                for pattern in patterns:
                    try:
                        matches = root.glob(pattern)
                    except (OSError, ValueError) as exc:
                        errors.append(f"{provider}: discovery pattern failed: {type(exc).__name__}")
                        continue
                    for path in matches:
                        if len(candidates) >= discovery_policy.max_files:
                            skipped += 1
                            continue
                        try:
                            if path.is_symlink() or not path.is_file():
                                skipped += 1
                                continue
                            normalized_path = path.resolve(strict=False)
                            if normalized_path in seen_paths:
                                continue
                            seen_paths.add(normalized_path)
                            stat = path.stat()
                            if stat.st_size > discovery_policy.max_file_bytes:
                                skipped += 1
                                continue
                            file_hash = _hash_file_bounded(path, discovery_policy.max_file_bytes)
                        except _InputTooLargeError:
                            skipped += 1
                            continue
                        except OSError:
                            skipped += 1
                            continue
                        candidates.append(
                            SessionCandidate(
                                provider=provider,
                                format=format_id,
                                path=path,
                                session_id=_candidate_session_id(provider, path, file_hash),
                                modified_at=float(stat.st_mtime),
                                size_bytes=int(stat.st_size),
                                file_sha256=file_hash,
                            )
                        )

        ordered = tuple(
            sorted(candidates, key=lambda item: (item.modified_at, str(item.path)), reverse=True)
        )
        latest = ordered[0].modified_at if ordered else None
        status: SessionImportStatus = "partial" if errors else "ok"
        return SessionDiscoveryResult(
            candidates=ordered,
            report=SessionDiscoveryReport(
                status=status,
                roots_scanned=len(roots),
                candidates_found=len(ordered),
                skipped_files=skipped,
                latest_modified_at=latest,
                errors=tuple(errors),
            ),
        )

    def import_discovered_sessions(
        self,
        *,
        project: str,
        discovery_policy: SessionDiscoveryPolicy | None = None,
        import_policy: SessionImportPolicy | None = None,
        known_file_hashes: set[str] | frozenset[str] | tuple[str, ...] = (),
        dry_run: bool = True,
    ) -> SessionAutoImportResult:
        """Discover and optionally import session JSONL files with SHA-256 deduplication."""
        try:
            project_id = _validate_identifier(project, "project")
        except ValueError as exc:
            return SessionAutoImportResult(
                candidates=(),
                records=(),
                manifests=(),
                report=SessionAutoImportReport(status="error", dry_run=dry_run, errors=(str(exc),)),
            )

        discovery = self.discover_sessions(discovery_policy)
        seen_hashes = {str(value) for value in known_file_hashes}
        records: list[dict[str, Any]] = []
        manifests: list[dict[str, Any]] = []
        errors = list(discovery.report.errors)
        imported = 0
        duplicate_files = 0
        malformed = 0
        redacted = 0

        for candidate in discovery.candidates:
            if candidate.file_sha256 in seen_hashes:
                duplicate_files += 1
                continue
            seen_hashes.add(candidate.file_sha256)
            if dry_run:
                continue
            result = self.import_file(
                candidate.path,
                project=project_id,
                session_id=candidate.session_id,
                format=candidate.format,
                policy=import_policy,
            )
            if result.report.status == "error":
                errors.extend(result.report.errors)
                continue
            imported += 1
            malformed += result.report.malformed_records
            redacted += result.report.redacted_fields
            manifests.append(result.manifest)
            records.extend(result.records)
            if result.report.errors:
                errors.extend(result.report.errors)

        status: SessionImportStatus = (
            ("partial" if records or dry_run else "error") if errors else "ok"
        )
        return SessionAutoImportResult(
            candidates=discovery.candidates,
            records=tuple(records),
            manifests=tuple(manifests),
            report=SessionAutoImportReport(
                status=status,
                dry_run=dry_run,
                candidates_found=len(discovery.candidates),
                files_imported=imported,
                duplicate_files=duplicate_files,
                records_emitted=len(records),
                malformed_records=malformed,
                redacted_fields=redacted,
                errors=tuple(errors),
            ),
        )

    def poll_discovered_sessions(
        self,
        *,
        project: str,
        discovery_policy: SessionDiscoveryPolicy | None = None,
        import_policy: SessionImportPolicy | None = None,
        interval_seconds: float = 5.0,
        iterations: int | None = None,
        dry_run: bool = False,
    ) -> Iterator[SessionAutoImportResult]:
        """Long-running polling entrypoint for embedders that want a daemon loop."""
        if interval_seconds < 0:
            raise ValueError("poll interval must be non-negative")
        if iterations is not None and iterations <= 0:
            raise ValueError("iterations must be positive when supplied")
        seen_hashes: set[str] = set()
        count = 0
        while iterations is None or count < iterations:
            result = self.import_discovered_sessions(
                project=project,
                discovery_policy=discovery_policy,
                import_policy=import_policy,
                known_file_hashes=seen_hashes,
                dry_run=dry_run,
            )
            seen_hashes.update(candidate.file_sha256 for candidate in result.candidates)
            yield result
            count += 1
            if iterations is None or count < iterations:
                time.sleep(interval_seconds)

    def _import_jsonl(
        self,
        source: Path,
        *,
        project: str,
        session_id: str,
        format: str = SESSION_FORMAT,
        policy: SessionImportPolicy,
    ) -> SessionImportResult:
        try:
            data = _read_bounded(source, policy.max_file_bytes)
        except _InputTooLargeError:
            return _failed("jsonl", "session file max_file_bytes limit exceeded")
        except (OSError, ValueError):
            return _failed("jsonl", "session file cannot be read safely")

        namespace = f"project/{project}/session/{session_id}"
        records: list[dict[str, Any]] = []
        errors: list[str] = []
        seen = 0
        malformed = 0
        redacted = 0
        for line_number, raw_line in enumerate(data.splitlines(), start=1):
            if not raw_line.strip():
                continue
            seen += 1
            if len(raw_line) > policy.max_line_bytes:
                malformed += 1
                errors.append(f"line {line_number}: line size limit exceeded")
                continue
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                malformed += 1
                errors.append(f"line {line_number}: invalid UTF-8")
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                errors.append(f"line {line_number}: malformed JSON")
                continue
            if not isinstance(payload, Mapping):
                malformed += 1
                errors.append(f"line {line_number}: record must be an object")
                continue
            version = _version(payload.get("schema_version", payload.get("version", "1")))
            if version not in policy.supported_versions:
                malformed += 1
                errors.append(f"line {line_number}: unsupported schema version")
                continue
            if len(records) >= policy.max_records:
                errors.append("record limit exceeded")
                break
            try:
                record, changed = _normalize_record(
                    _flatten_provider_payload(payload),
                    line_number=line_number,
                    project=project,
                    session_id=session_id,
                    namespace=namespace,
                    input_version=version,
                    source_format=format,
                    policy=policy,
                )
            except ValueError as exc:
                malformed += 1
                errors.append(f"line {line_number}: {exc}")
                continue
            records.append(record)
            redacted += changed

        status: SessionImportStatus = "partial" if errors else "ok"
        manifest = _build_manifest(
            project=project,
            session_id=session_id,
            namespace=namespace,
            records=records,
            format=format,
        )
        report = SessionImportReport(
            format=format,
            status=status,
            records_seen=seen,
            records_emitted=len(records),
            malformed_records=malformed,
            skipped_records=malformed,
            redacted_fields=redacted,
            errors=tuple(errors),
        )
        return SessionImportResult(tuple(records), manifest, report)


def import_session_jsonl(
    source: str | Path, *, project: str, session_id: str, policy: SessionImportPolicy | None = None
) -> SessionImportResult:
    """Convenience wrapper for the supported JSONL session format."""
    return SessionAdapter().import_file(
        source, project=project, session_id=session_id, format="jsonl", policy=policy
    )


def normalize_session_record(
    raw: Mapping[str, Any], *, line_number: int, project: str, session_id: str, redact: bool = True
) -> dict[str, Any] | None:
    """Normalize one record for stream consumers without opening a file."""
    try:
        project_id = _validate_identifier(project, "project")
        session_key = _validate_identifier(session_id, "session_id")
        record, _ = _normalize_record(
            raw,
            line_number=line_number,
            project=project_id,
            session_id=session_key,
            namespace=f"project/{project_id}/session/{session_key}",
            input_version=SESSION_SCHEMA_VERSION,
            source_format=SESSION_FORMAT,
            policy=SessionImportPolicy(include_raw_prompts=not redact),
        )
        return record
    except (ValueError, TypeError):
        return None


def session_record_to_memory_record(record: Mapping[str, Any]) -> MemoryRecord:
    """Convert one normalized session record to a strict durable record.

    Provider-specific fields remain in metadata while the durable store sees
    only the canonical :class:`MemoryRecord` shape.
    """
    required = ("record_id", "namespace", "key", "content", "content_hash")
    if any(key not in record for key in required):
        raise ValueError("normalized session record is missing canonical fields")
    metadata = dict(record.get("metadata") or {})
    for key in (
        "project",
        "session_id",
        "sequence",
        "record_type",
        "role",
        "event_type",
        "tool_name",
    ):
        if key in record:
            metadata.setdefault(key, record[key])
    return MemoryRecord(
        record_id=str(record["record_id"]),
        namespace=str(record["namespace"]),
        key=str(record["key"]),
        content=str(record["content"]),
        source=str(record.get("source", "session-jsonl")),
        trust=str(record.get("trust", "local-observation")),
        scope=str(record.get("scope", record["namespace"])),
        metadata=metadata,
        created_at=float(record.get("created_at", 0.0)),
        expires_at=record.get("expires_at"),
        supersedes=record.get("supersedes"),
        authority=str(record.get("authority", "unverified")),
        authority_verified=False,
        confidence=float(record.get("confidence", 1.0)),
        sensitivity=str(record.get("sensitivity", "standard")),
        provenance=dict(record.get("provenance") or {}),
        updated_at=float(record.get("updated_at", 0.0)),
        content_hash=str(record["content_hash"]),
        status="active",
    )


def import_session(
    source: str | Path,
    *,
    project: str,
    session_id: str,
    format: str | None = None,
    policy: SessionImportPolicy | None = None,
) -> SessionImportResult:
    """Format-dispatching entry point with explicit unavailable outcomes."""
    return SessionAdapter().import_file(
        source, project=project, session_id=session_id, format=format, policy=policy
    )


def discover_sessions(policy: SessionDiscoveryPolicy | None = None) -> SessionDiscoveryResult:
    """Convenience wrapper for bounded provider-neutral session discovery."""
    return SessionAdapter().discover_sessions(policy)


def import_discovered_sessions(
    *,
    project: str,
    discovery_policy: SessionDiscoveryPolicy | None = None,
    import_policy: SessionImportPolicy | None = None,
    known_file_hashes: set[str] | frozenset[str] | tuple[str, ...] = (),
    dry_run: bool = True,
) -> SessionAutoImportResult:
    """Convenience wrapper for SHA-256/idempotent automatic discovery import."""
    return SessionAdapter().import_discovered_sessions(
        project=project,
        discovery_policy=discovery_policy,
        import_policy=import_policy,
        known_file_hashes=known_file_hashes,
        dry_run=dry_run,
    )


def poll_discovered_sessions(
    *,
    project: str,
    discovery_policy: SessionDiscoveryPolicy | None = None,
    import_policy: SessionImportPolicy | None = None,
    interval_seconds: float = 5.0,
    iterations: int | None = None,
    dry_run: bool = False,
) -> Iterator[SessionAutoImportResult]:
    """Convenience wrapper for embedders wiring a polling/daemon loop."""
    return SessionAdapter().poll_discovered_sessions(
        project=project,
        discovery_policy=discovery_policy,
        import_policy=import_policy,
        interval_seconds=interval_seconds,
        iterations=iterations,
        dry_run=dry_run,
    )


def _normalize_record(
    payload: Mapping[str, Any],
    *,
    line_number: int,
    project: str,
    session_id: str,
    namespace: str,
    input_version: str,
    source_format: str = SESSION_FORMAT,
    policy: SessionImportPolicy,
) -> tuple[dict[str, Any], int]:
    raw_role = payload.get("role")
    role = _normalize_role(raw_role) if isinstance(raw_role, str) else None
    raw_type = _first_string(payload, "event_type", "type", "event", "kind")
    tool_name = _first_string(payload, "tool_name", "tool", "name")
    is_tool = bool(raw_type and raw_type.lower() in _TOOL_EVENTS) or (
        tool_name is not None and role == "tool"
    )
    if role is None and not is_tool and raw_type is None:
        raise ValueError("record has no role or event type")
    if is_tool:
        record_type = "tool_event"
        canonical_role = "tool"
        content_key = _first_key(payload, "arguments", "args", "input", "result", "output", "data")
        raw_content = payload.get(content_key) if content_key else None
        if raw_content is None:
            raw_content = payload.get("content", payload.get("message"))
    else:
        record_type = "message"
        canonical_role = role or "event"
        content_key = _first_key(payload, "content", "text", "message", "prompt")
        raw_content = payload.get(content_key) if content_key else None
    if raw_content is None:
        raw_content = ""
    if not isinstance(raw_content, (str, Mapping, list, int, float, bool)):
        raise ValueError("record content is not a supported value")

    raw_content_text = (
        _canonical_json(raw_content) if not isinstance(raw_content, str) else raw_content
    )
    safe_content: Any
    changed = 0
    if canonical_role == "user" and not policy.include_raw_prompts:
        safe_content = "[redacted]"
        changed += 1
    else:
        safe_content, content_changed = _redact_value(
            raw_content, key=content_key or "content", redact_credentials=policy.redact_credentials
        )
        changed += content_changed
    if not isinstance(safe_content, str):
        safe_content = _canonical_json(safe_content)
    if not safe_content:
        safe_content = "[empty]"
    content_hash = hashlib.sha256(safe_content.encode("utf-8")).hexdigest()
    metadata: dict[str, Any] = {
        "project": project,
        "session_id": session_id,
        "namespace": namespace,
        "source_format": source_format,
        "source_line": line_number,
        "input_schema_version": input_version,
    }
    raw_metadata = payload.get("metadata")
    if isinstance(raw_metadata, Mapping):
        safe_metadata, metadata_changed = _redact_value(
            raw_metadata, key="metadata", redact_credentials=policy.redact_credentials
        )
        metadata["attributes"] = safe_metadata
        changed += metadata_changed
    if raw_type is not None:
        metadata["event_type"] = raw_type
    if tool_name is not None:
        safe_tool, tool_changed = _redact_value(
            tool_name, key="tool_name", redact_credentials=policy.redact_credentials
        )
        metadata["tool_name"] = safe_tool
        changed += tool_changed
    identity = {
        "namespace": namespace,
        "source_line": line_number,
        "record_type": record_type,
        "role": canonical_role,
        "content_hash": content_hash,
        "event_type": raw_type,
        "tool_name": tool_name,
    }
    record_id = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    record: dict[str, Any] = {
        "schema_version": SESSION_SCHEMA_VERSION,
        "record_id": record_id,
        "namespace": namespace,
        "key": f"{session_id}:line-{line_number:08d}",
        "project": project,
        "session_id": session_id,
        "sequence": line_number,
        "record_type": record_type,
        "role": canonical_role,
        "event_type": raw_type or record_type,
        "content": safe_content,
        "source": "session-jsonl",
        "trust": "local-observation",
        "scope": namespace,
        "created_at": float(line_number),
        "updated_at": float(line_number),
        "expires_at": None,
        "supersedes": None,
        "authority": "unverified",
        "authority_verified": False,
        "confidence": 1.0,
        "sensitivity": "standard",
        "provenance": {
            "source": "session-jsonl",
            "adapter": SessionAdapter.adapter_id,
            "adapter_protocol_version": SESSION_ADAPTER_PROTOCOL_VERSION,
            "format": source_format,
            "source_line": line_number,
            "project": project,
            "session_id": session_id,
            "input_schema_version": input_version,
            "raw_content_hash": hashlib.sha256(raw_content_text.encode("utf-8")).hexdigest(),
        },
        "content_hash": content_hash,
        "metadata": metadata,
        "status": "active",
    }
    if tool_name is not None:
        record["tool_name"] = metadata["tool_name"]
    return record, changed


def _build_manifest(
    *,
    project: str,
    session_id: str,
    namespace: str,
    records: list[dict[str, Any]],
    format: str = "jsonl",
) -> dict[str, Any]:
    ordered = sorted(records, key=lambda record: (int(record["sequence"]), record["record_id"]))
    manifest = {
        "manifest_version": SESSION_SCHEMA_VERSION,
        "adapter_id": SessionAdapter.adapter_id,
        "adapter_protocol_version": SESSION_ADAPTER_PROTOCOL_VERSION,
        "format": format,
        "project": project,
        "session_id": session_id,
        "namespace": namespace,
        "records": ordered,
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return manifest


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def verify_session_manifest(manifest: Mapping[str, Any]) -> bool:
    """Verify a session manifest before offline replay/import."""
    supplied = manifest.get("manifest_hash")
    if not isinstance(supplied, str) or supplied != _manifest_hash(manifest):
        return False
    records = manifest.get("records")
    return isinstance(records, list) and all(
        isinstance(record, Mapping)
        and isinstance(record.get("content_hash"), str)
        and record.get("content_hash")
        == hashlib.sha256(str(record.get("content", "")).encode("utf-8")).hexdigest()
        for record in records
    )


def _flatten_provider_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize common Claude/Codex/Pi message wrappers before validation."""
    nested = payload.get("message")
    if not isinstance(nested, Mapping):
        nested = payload.get("item")
    if not isinstance(nested, Mapping):
        return dict(payload)
    result = dict(nested)
    result.update({key: value for key, value in payload.items() if key not in {"message", "item"}})
    return result


def _redact_value(value: Any, *, key: str, redact_credentials: bool) -> tuple[Any, int]:
    if _PROMPT_KEY.search(key):
        return "[redacted]", 1
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        changed = 0
        for raw_key, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
            name = str(raw_key)
            if _SECRET_KEY.search(name):
                result[name] = "[redacted]"
                changed += 1
                continue
            safe, count = _redact_value(raw_value, key=name, redact_credentials=redact_credentials)
            result[name] = safe
            changed += count
        return result, changed
    if isinstance(value, list):
        list_result: list[Any] = []
        changed = 0
        for item in value:
            safe, count = _redact_value(item, key=key, redact_credentials=redact_credentials)
            list_result.append(safe)
            changed += count
        return list_result, changed
    if isinstance(value, str) and redact_credentials and _SECRET_VALUE.search(value):
        return "[redacted]", 1
    return value, 0


def _read_bounded(path: Path, limit: int) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise OSError("source is not a regular file")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise _InputTooLargeError
    return data


def _hash_file_bounded(path: Path, limit: int) -> str:
    return hashlib.sha256(_read_bounded(path, limit)).hexdigest()


def _discovery_roots(policy: SessionDiscoveryPolicy) -> tuple[Path, ...]:
    if policy.roots:
        return tuple(Path(root) for root in policy.roots)
    roots = [Path.cwd()]
    home = Path.home()
    if home not in roots:
        roots.append(home)
    return tuple(roots)


def _candidate_session_id(provider: str, path: Path, file_hash: str) -> str:
    stem = path.stem
    for suffix in ("_transcript", "-transcript", ".transcript"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", stem).strip(".-")
    if not safe_stem:
        safe_stem = "session"
    candidate = f"{provider}-{safe_stem}-{file_hash[:12]}"
    return candidate[:128].rstrip(".-") or f"{provider}-{file_hash[:12]}"


def _select_format(source: str | Path, requested: str | None) -> str:
    if requested is not None:
        return requested.lower().lstrip(".")
    suffix = Path(source).suffix.lower().lstrip(".")
    return suffix or "unknown"


def _version(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return "invalid"
    return str(value)


def _normalize_role(role: str) -> str:
    value = role.strip().lower()
    return _ROLE_ALIASES.get(value, value or "unknown")


def _first_string(payload: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return (
                value.strip().lower()
                if key != "tool_name" and key != "tool" and key != "name"
                else value.strip()
            )
    return None


def _first_key(payload: Mapping[str, Any], *keys: str) -> str | None:
    return next((key for key in keys if key in payload), None)


def _validate_identifier(value: str, field_name: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"invalid {field_name}")
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _unavailable(
    format: str, reason: str, *, status: SessionImportStatus = "unavailable"
) -> SessionImportResult:
    report = SessionImportReport(format=format, status=status, errors=(reason,))
    manifest = {
        "manifest_version": SESSION_SCHEMA_VERSION,
        "adapter_id": SessionAdapter.adapter_id,
        "adapter_protocol_version": SESSION_ADAPTER_PROTOCOL_VERSION,
        "format": format,
        "records": [],
    }
    return SessionImportResult((), manifest, report)


def _failed(format: str, reason: str) -> SessionImportResult:
    return _unavailable(format, reason, status="error")


class _InputTooLargeError(Exception):
    pass


__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_DISCOVERY_FILES",
    "DEFAULT_MAX_LINE_BYTES",
    "DEFAULT_MAX_RECORDS",
    "SESSION_ADAPTER_PROTOCOL_VERSION",
    "SESSION_FORMAT",
    "SESSION_FORMAT_VERSION",
    "SESSION_SCHEMA_VERSION",
    "SessionAutoImportReport",
    "SessionAutoImportResult",
    "SessionCandidate",
    "SessionDiscoveryPolicy",
    "SessionDiscoveryReport",
    "SessionDiscoveryResult",
    "SessionAdapter",
    "SessionFormatDescriptor",
    "SessionImportPolicy",
    "SessionImportReport",
    "SessionImportResult",
    "discover_sessions",
    "import_discovered_sessions",
    "import_session",
    "import_session_jsonl",
    "normalize_session_record",
    "poll_discovered_sessions",
    "session_record_to_memory_record",
    "verify_session_manifest",
]
