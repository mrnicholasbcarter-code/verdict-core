"""Small deterministic helpers for the MasterDocs migration boundary."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from verdict.memory_masterdocs_contracts import (
    MasterDocsIngestionReport,
    MasterDocsIngestionResult,
    MasterDocsIssue,
)

_LANGUAGES = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".text": "text",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".py": "python",
    ".ts": "typescript",
    ".js": "javascript",
}
_QUARANTINED_PARTS = {
    ".cache",
    "cache",
    "caches",
    "generated",
    "gen",
    "temp",
    "temporary",
    "tmp",
    "vendor",
    "vendors",
}
_GENERATED_NAME = re.compile(r"(?:[._-](?:generated|gen|tmp|temp))$", re.IGNORECASE)


def empty_result(*, status: str, reason: str, timestamp: float = 0.0) -> MasterDocsIngestionResult:
    issue = MasterDocsIssue("<source>", status, reason)
    report = MasterDocsIngestionReport(status=status, ingest_timestamp=timestamp, issues=(issue,))
    return MasterDocsIngestionResult((), report)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def contains_symlink(root: Path, candidate: Path) -> bool:
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        return True
    current = root
    for part in parts:
        current /= part
        if current.is_symlink():
            return True
    return False


def normalize_text(payload: bytes) -> str:
    text = payload.decode("utf-8").lstrip("\ufeff")
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def chunk_spans(text: str, chunk_size: int) -> list[tuple[str, int, int]]:
    chunks: list[tuple[str, int, int]] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundary = text.rfind("\n\n", start + 1, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end]
        if chunk:
            chunks.append(
                (chunk, len(text[:start].encode("utf-8")), len(text[:end].encode("utf-8")))
            )
        start = end
        while (
            start < len(text)
            and text[start] == "\n"
            and start + 1 < len(text)
            and text[start + 1] == "\n"
        ):
            start += 2
    return chunks


def language_for_path(path: str) -> str:
    return _LANGUAGES.get(Path(path).suffix.lower(), "unknown")


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_quarantined(relative: str, *, allow_tmp: bool) -> bool:
    parts = tuple(part.lower() for part in Path(relative).parts[:-1])
    stem = Path(relative).stem
    # The literal is an intentional quarantine policy, not a temp-file use.
    if not allow_tmp and ("/tmp/" in f"/{relative.lower()}/" or "tmp" in parts):  # nosec B108
        return True
    quarantined_parts = _QUARANTINED_PARTS - ({"tmp", "temp", "temporary"} if allow_tmp else set())
    return bool(set(parts) & quarantined_parts or _GENERATED_NAME.search(stem))


__all__ = [
    "chunk_spans",
    "contains_symlink",
    "empty_result",
    "is_quarantined",
    "is_relative_to",
    "language_for_path",
    "normalize_text",
    "sha256",
]
