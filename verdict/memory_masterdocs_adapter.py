"""MasterDocsRAG database adapter for MemoryPlane (#127).

Canonicalizes raw documents and FTS rows from MasterDocsRAG SQLite databases
into safe, versioned MemoryRecord shapes without direct unvalidated access.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.memory_plane import MemoryPlane, MemoryRecord

MASTERDOCS_ADAPTER_VERSION = "1"


@dataclass(frozen=True)
class MasterDocsIngestionReport:
    """Report generated during MasterDocs database canonicalization."""

    total_found: int
    ingested: int
    quarantined: int
    content_hashes: tuple[str, ...]
    quarantined_paths: tuple[str, ...]


class MasterDocsAdapter:
    """Legacy SQLite importer requiring explicit local/export opt-in.

    The default adapter registry deliberately exposes only the manifest
    boundary.  This compatibility class remains available for callers that
    have an explicitly exported, allowlisted local SQLite artifact.
    """

    def __init__(self, allowlisted_roots: tuple[Path, ...] | None = None) -> None:
        self.allowlisted_roots = allowlisted_roots or (Path.cwd().resolve(),)

    def _is_safe_path(self, path_str: str, allow_tmp: bool = False) -> bool:
        """Validate path safety against quarantine and root rules."""
        if not path_str:
            return False
        norm = path_str.replace("\\", "/").lower()

        if not allow_tmp and ("/tmp" in norm or "\\tmp" in norm or norm.startswith("/tmp")):  # nosec B108: deliberate quarantine check
            return False
        if any(q in norm for q in ["/vendor/", "/generated/", "/temp/"]):
            return False

        p = Path(path_str)
        if not p.is_absolute():
            return True

        resolved = p.resolve()
        return any(resolved.is_relative_to(root) for root in self.allowlisted_roots)

    def canonicalize_db(
        self,
        db_path: str | Path,
        plane: MemoryPlane,
        allow_tmp: bool = False,
        limit: int = 1000,
        allow_legacy_sqlite: bool = False,
    ) -> MasterDocsIngestionReport:
        """Read an explicitly approved local/exported SQLite artifact."""
        if not allow_legacy_sqlite:
            raise ValueError(
                "private MasterDocs SQLite input is disabled; use a validated manifest "
                "or set allow_legacy_sqlite=True for an exported local artifact"
            )
        path = Path(db_path).resolve()
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"masterdocs_db_not_found:{path}")

        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row

        total_found = 0
        ingested = 0
        quarantined = 0
        hashes: list[str] = []
        quarantined_paths: list[str] = []

        try:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = {row["name"] for row in cursor.fetchall()}

            rows: list[dict[str, Any]] = []
            if "documents" in tables:
                c = conn.execute("SELECT id, path, content FROM documents LIMIT ?", (limit,))
                rows = [dict(r) for r in c.fetchall()]
            elif "doc_fts" in tables:
                c = conn.execute("SELECT rowid as id, path, content FROM doc_fts LIMIT ?", (limit,))
                rows = [dict(r) for r in c.fetchall()]
            else:
                for t in tables:
                    try:
                        c = conn.execute(f"SELECT * FROM {t} LIMIT ?", (limit,))  # nosec B608
                        r_list = [dict(r) for r in c.fetchall()]
                        if r_list and "content" in r_list[0]:
                            rows = r_list
                            break
                    except Exception:
                        continue

            total_found = len(rows)
            for r in rows:
                doc_id = str(r.get("id") or r.get("path") or "unknown")
                doc_path = str(r.get("path") or f"masterdocs:{doc_id}")
                content = str(r.get("content") or "")

                if not content.strip():
                    continue

                if not self._is_safe_path(doc_path, allow_tmp=allow_tmp):
                    quarantined += 1
                    quarantined_paths.append(doc_path)
                    continue

                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                record = MemoryRecord(
                    record_id=f"rec_md_{doc_id}",
                    namespace="masterdocs",
                    key=f"doc:{doc_id}",
                    content=content,
                    source=f"masterdocs:{doc_path}",
                    content_hash=content_hash,
                    authority="masterdocs_rag",
                    confidence=1.0,
                    sensitivity="public",
                    scope="default",
                    provenance={
                        "source": f"masterdocs:{doc_path}",
                        "adapter": "masterdocs-sqlite-legacy",
                        "adapter_version": MASTERDOCS_ADAPTER_VERSION,
                        "schema_version": 2,
                        "db_path": str(path),
                        "doc_path": doc_path,
                        "content_hash": content_hash,
                        "authority": "masterdocs_rag",
                    },
                )
                plane.put(record)
                ingested += 1
                hashes.append(content_hash)

        finally:
            conn.close()

        return MasterDocsIngestionReport(
            total_found=total_found,
            ingested=ingested,
            quarantined=quarantined,
            content_hashes=tuple(hashes),
            quarantined_paths=tuple(quarantined_paths),
        )


__all__ = ["MASTERDOCS_ADAPTER_VERSION", "MasterDocsAdapter", "MasterDocsIngestionReport"]
