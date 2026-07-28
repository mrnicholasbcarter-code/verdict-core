"""Legacy Memory Archival, Ingestion, and Erasure Engine.

Detects OpenViking stores, legacy RAG DBs, uncoordinated tool memory dumps,
archives them into compressed tarballs, ingests valid records into the local
Verdict MemoryPlane (~/.verdict/memory.db), and safely erases legacy artifacts.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tarfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from verdict.memory_plane import MemoryPlane, MemoryRecord

LEGACY_PATTERNS = (
    ".openviking",
    "repair-openviking",
    ".pi_rag_context.json",
    "MasterDocsRAG.db",
    ".code-review-graph",
    ".codex-memory",
)


@dataclass(frozen=True)
class LegacyArtifactReport:
    """Report of detected legacy memory artifacts on host."""

    detected_paths: tuple[Path, ...]
    total_bytes: int
    openviking_found: bool


def detect_legacy_memory_artifacts(
    cwd: Path | None = None, home_dir: Path | None = None
) -> LegacyArtifactReport:
    """Scan root directory and home directory for legacy memory stores."""
    root = (cwd or Path.cwd()).resolve()
    home = (home_dir or Path.home()).resolve()

    found: list[Path] = []
    total_bytes = 0
    openviking = False

    search_dirs = [root, home]
    seen: set[Path] = set()

    for base in search_dirs:
        if not base.exists():
            continue
        for p in base.iterdir():
            if p in seen:
                continue
            seen.add(p)
            name = p.name.lower()
            if any(pat.lower() in name for pat in LEGACY_PATTERNS):
                found.append(p)
                if "openviking" in name:
                    openviking = True
                if p.is_file():
                    total_bytes += p.stat().st_size
                elif p.is_dir():
                    for sub in p.rglob("*"):
                        if sub.is_file():
                            total_bytes += sub.stat().st_size

    return LegacyArtifactReport(
        detected_paths=tuple(sorted(found, key=lambda x: str(x))),
        total_bytes=total_bytes,
        openviking_found=openviking,
    )


def archive_legacy_memory(
    report: LegacyArtifactReport, output_dir: Path | None = None
) -> Path | None:
    """Bundle detected legacy memory files into a timestamped gzip tarball."""
    if not report.detected_paths:
        return None

    target_dir = (output_dir or Path.cwd() / ".verdict" / "archive").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = target_dir / f"memory_archive_{timestamp}.tar.gz"

    with tarfile.open(archive_path, "w:gz") as tar:
        for p in report.detected_paths:
            if p.exists():
                tar.add(p, arcname=p.name)

    return archive_path


def ingest_legacy_artifacts(report: LegacyArtifactReport, plane: MemoryPlane) -> int:
    """Ingest valid memory records from legacy artifacts into MemoryPlane."""
    ingested_count = 0

    for path in report.detected_paths:
        if not path.exists():
            continue

        if path.is_file() and path.name.endswith(".json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                content_str = json.dumps(data, sort_keys=True)
                content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
                record = MemoryRecord(
                    record_id=f"rec_legacy_json_{path.stem}",
                    namespace="legacy_rag",
                    key=f"legacy:{path.name}",
                    content=content_str[:50000],
                    source=f"legacy_file:{path.name}",
                    content_hash=content_hash,
                    authority="migration",
                    confidence=0.8,
                    sensitivity="internal",
                    provenance={"original_path": str(path)},
                )
                plane.put(record)
                ingested_count += 1
            except Exception:
                pass

        elif path.is_file() and path.name.endswith(".db"):
            try:
                conn = sqlite3.connect(str(path))
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table';"
                )
                tables = {row["name"] for row in cursor.fetchall()}
                for tbl in list(tables)[:5]:
                    c = conn.execute(f"SELECT * FROM {tbl} LIMIT 100")  # nosec B608
                    rows = [dict(r) for r in c.fetchall()]
                    if rows:
                        content_str = json.dumps(rows, default=str)
                        content_hash = hashlib.sha256(content_str.encode("utf-8")).hexdigest()
                        record = MemoryRecord(
                            record_id=f"rec_legacy_db_{path.stem}_{tbl}",
                            namespace="legacy_rag",
                            key=f"legacy_db:{path.name}:{tbl}",
                            content=content_str[:50000],
                            source=f"legacy_db:{path.name}",
                            content_hash=content_hash,
                            authority="migration",
                            confidence=0.8,
                            sensitivity="internal",
                            provenance={"db": path.name, "table": tbl},
                        )
                        plane.put(record)
                        ingested_count += 1
                conn.close()
            except Exception:
                pass

    return ingested_count


def purge_legacy_artifacts(report: LegacyArtifactReport) -> tuple[Path, ...]:
    """Safely erase legacy files and directories after archival and ingestion."""
    removed: list[Path] = []

    for path in report.detected_paths:
        if not path.exists():
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed.append(path)
            elif path.is_dir():
                shutil.rmtree(path)
                removed.append(path)
        except Exception:
            pass

    return tuple(removed)


__all__ = [
    "LegacyArtifactReport",
    "archive_legacy_memory",
    "detect_legacy_memory_artifacts",
    "ingest_legacy_artifacts",
    "purge_legacy_artifacts",
]
