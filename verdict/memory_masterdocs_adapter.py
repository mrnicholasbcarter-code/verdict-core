"""Explicit, provenance-safe migration from legacy MasterDocs SQLite files.

MasterDocsRAG is an optional legacy source, never a MemoryPlane authority.  A
caller must explicitly opt into the SQLite migration boundary; the importer
then emits deterministic canonical chunks and a machine-readable report.  It
does not modify the source database and refuses the divergent ``corpus_fts``
shape unless a future, versioned migration is added.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from verdict.memory_masterdocs_contracts import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_CONTENT_BYTES,
    MASTERDOCS_ADAPTER_VERSION,
    MASTERDOCS_SCHEMA_VERSION,
    MasterDocsIngestionReport,
    MasterDocsIngestionResult,
    MasterDocsIssue,
)
from verdict.memory_masterdocs_support import (
    chunk_spans,
    contains_symlink,
    empty_result,
    is_quarantined,
    is_relative_to,
    language_for_path,
    normalize_text,
    sha256,
)
from verdict.memory_plane import MemoryPlane


@dataclass(frozen=True)
class _Document:
    root_id: str
    relative_path: str
    content: str
    document_hash: str
    language: str
    source_paths: tuple[str, ...]


class MasterDocsAdapter:
    """Migrate an explicitly approved legacy SQLite export into MemoryPlane."""

    def __init__(
        self,
        allowlisted_roots: tuple[Path, ...] | None = None,
        *,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        roots = tuple(Path(root).expanduser() for root in (allowlisted_roots or (Path.cwd(),)))
        if not roots or any(not root.is_absolute() for root in roots):
            raise ValueError("allowlisted roots must be absolute")
        if max_content_bytes <= 0 or chunk_size <= 0:
            raise ValueError("MasterDocs limits must be positive")
        self.allowlisted_roots = tuple(root.resolve() for root in roots)
        self.max_content_bytes = max_content_bytes
        self.chunk_size = chunk_size

    def _is_safe_path(self, path_str: str, allow_tmp: bool = False) -> bool:
        """Retain the compatibility predicate used by older callers."""
        return self._path_info(path_str, allow_tmp=allow_tmp) is not None

    def _path_info(self, path_str: str, *, allow_tmp: bool) -> tuple[str, str] | None:
        if not path_str or "\x00" in path_str:
            return None
        normalized = path_str.replace("\\", "/")
        path = Path(normalized)
        # The literal is an intentional quarantine policy, not a temp-file use.
        if not allow_tmp and normalized.lower().startswith("/tmp/"):  # nosec B108
            return None
        if path.is_absolute():
            resolved = path.resolve()
            matches = [
                (index, root)
                for index, root in enumerate(self.allowlisted_roots)
                if is_relative_to(resolved, root)
            ]
            if not matches:
                return None
            index, root = max(matches, key=lambda item: len(item[1].parts))
            if contains_symlink(root, resolved):
                return None
            relative = resolved.relative_to(root).as_posix()
        else:
            index = 0
            relative = Path(normalized).as_posix()
            if relative.startswith("../") or relative == "..":
                return None
        parts = tuple(part.lower() for part in Path(relative).parts[:-1])
        if not allow_tmp and ("/tmp/" in f"/{relative.lower()}/" or "tmp" in parts):  # nosec B108
            return None
        if is_quarantined(relative, allow_tmp=allow_tmp):
            return None
        return f"root-{index}", relative

    def canonicalize_db_records(
        self,
        db_path: str | Path,
        *,
        allow_tmp: bool = False,
        limit: int = 1_000,
        allow_legacy_sqlite: bool = False,
        ingest_timestamp: float | None = None,
    ) -> MasterDocsIngestionResult:
        """Read and canonicalize rows without writing any destination state."""
        if not allow_legacy_sqlite:
            return empty_result(
                status="unavailable",
                reason="private MasterDocs SQLite input is disabled; use a validated manifest",
            )
        if limit <= 0:
            raise ValueError("limit must be positive")
        path = Path(db_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"masterdocs_db_not_found:{path}")
        # A zero timestamp is the deterministic replay value.  Operators that
        # need wall-clock provenance can provide an explicit timestamp; it is
        # never inferred from the host clock during an offline rebuild.
        timestamp = 0.0 if ingest_timestamp is None else float(ingest_timestamp)
        if timestamp < 0:
            raise ValueError("ingest_timestamp must be non-negative")
        raw_path = Path(db_path).expanduser()
        if raw_path.is_symlink() or not any(
            is_relative_to(path, root) for root in self.allowlisted_roots
        ):
            return empty_result(
                status="rejected",
                reason="SQLite source is outside the allowlisted roots",
                timestamp=timestamp,
            )

        issues: list[MasterDocsIssue] = []
        raw_rows: list[dict[str, Any]] = []
        try:
            database_uri = f"file:{quote(str(path), safe='/')}?mode=ro"
            with sqlite3.connect(database_uri, uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                }
                if "corpus_fts" in tables:
                    return empty_result(
                        status="rejected",
                        reason="corpus_fts requires an explicit versioned migration",
                        timestamp=timestamp,
                    )
                table = next(
                    (candidate for candidate in ("documents", "doc_fts") if candidate in tables),
                    None,
                )
                if table is None:
                    return empty_result(
                        status="rejected",
                        reason="unsupported MasterDocs schema; expected documents or doc_fts",
                        timestamp=timestamp,
                    )
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})")  # nosec B608
                }
                if not {"path", "content"}.issubset(columns):
                    return empty_result(
                        status="rejected",
                        reason=f"{table} schema must contain path and content",
                        timestamp=timestamp,
                    )
                raw_rows = [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT path, content FROM {table} ORDER BY rowid LIMIT ?",  # nosec B608
                        (limit,),
                    )
                ]
        except sqlite3.DatabaseError as exc:
            return empty_result(
                status="rejected",
                reason=f"invalid SQLite source: {exc.__class__.__name__}",
                timestamp=timestamp,
            )

        documents: list[_Document] = []
        by_path: dict[tuple[str, str], _Document] = {}
        by_hash: dict[str, int] = {}
        bytes_read = 0
        duplicates = 0
        skipped = 0
        rejected = 0
        quarantined = 0
        for row in raw_rows:
            raw_path_value: Any = row.get("path")
            raw_content = row.get("content")
            path_text = str(raw_path_value) if raw_path_value is not None else ""
            if not isinstance(raw_content, (str, bytes)):
                issues.append(MasterDocsIssue(path_text, "rejected", "content is not text"))
                rejected += 1
                continue
            payload = raw_content.encode("utf-8") if isinstance(raw_content, str) else raw_content
            bytes_read += len(payload)
            if len(payload) > self.max_content_bytes:
                issues.append(
                    MasterDocsIssue(path_text, "rejected", "content exceeds max_content_bytes")
                )
                rejected += 1
                continue
            info = self._path_info(path_text, allow_tmp=allow_tmp)
            if info is None:
                issues.append(MasterDocsIssue(path_text, "quarantined", "path is outside policy"))
                quarantined += 1
                continue
            root_id, relative_path = info
            try:
                content = normalize_text(payload)
            except UnicodeDecodeError:
                issues.append(
                    MasterDocsIssue(relative_path, "rejected", "content is not valid UTF-8")
                )
                rejected += 1
                continue
            if not content:
                issues.append(MasterDocsIssue(relative_path, "skipped", "content is empty"))
                skipped += 1
                continue
            document_hash = sha256(content)
            source_identity = (root_id, relative_path)
            previous = by_path.get(source_identity)
            if previous is not None:
                if previous.document_hash == document_hash:
                    duplicates += 1
                    issues.append(
                        MasterDocsIssue(relative_path, "duplicate", "identical source row")
                    )
                else:
                    rejected += 1
                    issues.append(
                        MasterDocsIssue(
                            relative_path, "conflict", "same path has different content"
                        )
                    )
                continue
            existing_index = by_hash.get(document_hash)
            if existing_index is not None:
                duplicate = documents[existing_index]
                documents[existing_index] = _Document(
                    duplicate.root_id,
                    duplicate.relative_path,
                    duplicate.content,
                    duplicate.document_hash,
                    duplicate.language,
                    tuple(sorted({*duplicate.source_paths, relative_path})),
                )
                by_path[source_identity] = documents[existing_index]
                duplicates += 1
                issues.append(
                    MasterDocsIssue(
                        relative_path, "duplicate", "content matches another source path"
                    )
                )
                continue
            document = _Document(
                root_id,
                relative_path,
                content,
                document_hash,
                language_for_path(relative_path),
                (relative_path,),
            )
            by_path[source_identity] = document
            by_hash[document_hash] = len(documents)
            documents.append(document)

        records: list[dict[str, Any]] = []
        for document in documents:
            chunks = chunk_spans(document.content, self.chunk_size)
            for chunk_index, (chunk, start, end) in enumerate(chunks):
                chunk_hash = sha256(chunk)
                record_identity = "\0".join(
                    (
                        document.root_id,
                        document.relative_path,
                        document.document_hash,
                        str(chunk_index),
                        chunk_hash,
                    )
                )
                record_id = sha256(record_identity)
                provenance = {
                    "source": "masterdocs-sqlite-legacy",
                    "adapter": "masterdocs-sqlite",
                    "adapter_version": MASTERDOCS_ADAPTER_VERSION,
                    "schema_version": MASTERDOCS_SCHEMA_VERSION,
                    "source_root_id": document.root_id,
                    "relative_path": document.relative_path,
                    "source_paths": list(document.source_paths),
                    "language": document.language,
                    "extractor_version": MASTERDOCS_ADAPTER_VERSION,
                    "chunker_version": MASTERDOCS_SCHEMA_VERSION,
                    "document_hash": document.document_hash,
                    "chunk_hash": chunk_hash,
                    "byte_start": start,
                    "byte_end": end,
                    "ingest_timestamp": timestamp,
                }
                records.append(
                    {
                        "record_id": record_id,
                        "namespace": "masterdocs",
                        "key": f"{document.relative_path}#chunk-{chunk_index:04d}",
                        "content": chunk,
                        "source": f"masterdocs:{document.root_id}/{document.relative_path}",
                        "trust": "imported-unverified",
                        "scope": "default",
                        "metadata": {
                            "language": document.language,
                            "source_root_id": document.root_id,
                            "relative_path": document.relative_path,
                            "source_paths": list(document.source_paths),
                            "extractor_version": MASTERDOCS_ADAPTER_VERSION,
                            "chunker_version": MASTERDOCS_SCHEMA_VERSION,
                            "byte_start": start,
                            "byte_end": end,
                            "ingest_timestamp": timestamp,
                        },
                        "created_at": timestamp,
                        "updated_at": timestamp,
                        "expires_at": None,
                        "supersedes": None,
                        "authority": "untrusted",
                        "authority_verified": False,
                        "confidence": 1.0,
                        "sensitivity": "standard",
                        "provenance": provenance,
                        "content_hash": chunk_hash,
                        "schema_version": 2,
                        "status": "active",
                    }
                )

        status = "empty" if not raw_rows or not documents else "ok" if not issues else "partial"
        if rejected and not records:
            status = "rejected"
        hashes = tuple(record["content_hash"] for record in records)
        report = MasterDocsIngestionReport(
            status=status,
            total_found=len(raw_rows),
            documents_seen=len(raw_rows),
            documents_accepted=len(documents),
            chunks_emitted=len(records),
            ingested=0,
            quarantined=quarantined,
            rejected=rejected,
            duplicates=duplicates,
            skipped=skipped,
            bytes_read=bytes_read,
            ingest_timestamp=timestamp,
            content_hashes=hashes,
            quarantined_paths=tuple(
                issue.path for issue in issues if issue.category == "quarantined"
            ),
            issues=tuple(issues),
        )
        digest = sha256(
            json.dumps(
                {"records": records, "report": report.to_dict()},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return MasterDocsIngestionResult(
            records=tuple(records), report=replace(report, manifest_hash=digest)
        )

    def canonicalize_db(
        self,
        db_path: str | Path,
        plane: MemoryPlane,
        allow_tmp: bool = False,
        limit: int = 1_000,
        allow_legacy_sqlite: bool = False,
        ingest_timestamp: float | None = None,
    ) -> MasterDocsIngestionReport:
        """Canonicalize and explicitly import into a caller-owned MemoryPlane."""
        result = self.canonicalize_db_records(
            db_path,
            allow_tmp=allow_tmp,
            limit=limit,
            allow_legacy_sqlite=allow_legacy_sqlite,
            ingest_timestamp=ingest_timestamp,
        )
        return self.import_result(result, plane)

    def import_result(
        self, result: MasterDocsIngestionResult, plane: MemoryPlane
    ) -> MasterDocsIngestionReport:
        """Write an already-canonicalized result into a caller-owned plane."""
        if result.report.status == "unavailable":
            return result.report
        written = 0
        duplicates = 0
        errors = list(result.report.issues)
        for record in result.memory_records:
            try:
                existing = plane.get(record.namespace, record.key, scope=record.scope)
                plane.put(record)
                if existing is not None and existing.content_hash == record.content_hash:
                    duplicates += 1
                else:
                    written += 1
            except (TypeError, ValueError) as exc:
                errors.append(MasterDocsIssue(record.key, "rejected", str(exc)[:256]))
        status = result.report.status
        if len(errors) > len(result.report.issues):
            status = "rejected" if not written else "partial"
        return replace(
            result.report,
            status=status,
            ingested=written,
            duplicates=result.report.duplicates + duplicates,
            issues=tuple(errors),
        )


__all__ = [
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_MAX_CONTENT_BYTES",
    "MASTERDOCS_ADAPTER_VERSION",
    "MASTERDOCS_SCHEMA_VERSION",
    "MasterDocsAdapter",
    "MasterDocsIngestionReport",
    "MasterDocsIngestionResult",
    "MasterDocsIssue",
]
