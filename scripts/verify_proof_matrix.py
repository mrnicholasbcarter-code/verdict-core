#!/usr/bin/env python3
"""Validate the public proof matrix and claims ledger without network access."""

from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
PROOF_DIR = ROOT / "docs" / "proof"
MATRIX_PATH = PROOF_DIR / "proof_matrix.v1.json"
LEDGER_PATH = PROOF_DIR / "claims_ledger.v1.json"
_SECRET = re.compile(
    r"(?i)(?:sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._~+/=-]{8,}|(?:api[_-]?key|password|secret|token)\s*[=:]\s*[^\s,;]+)"
)
_ALLOWED_MATRIX_STATUSES = {"verified", "observed", "partial", "blocked", "not_started"}
_ALLOWED_LEDGER_STATUSES = {
    "verified",
    "observed",
    "self_reported",
    "inferred",
    "aspiration",
    "unsupported",
}


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path.relative_to(ROOT)}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain an object")
    return value


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _validate_path(value: Any, *, field: str, require_exists: bool = True) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{field} must be a repository-relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{field} must not be absolute or traverse directories")
    resolved = (ROOT / path).resolve()
    if ROOT.resolve() not in resolved.parents:
        raise ValueError(f"{field} escapes the repository")
    if require_exists and not resolved.is_file():
        raise ValueError(f"{field} does not exist: {value}")
    return resolved


def _validate_text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if _SECRET.search(value):
        raise ValueError(f"{field} contains a secret-bearing pattern")


def _validate_common(document: dict[str, Any], title: str) -> tuple[date, date]:
    if document.get("schema_version") != 1:
        raise ValueError(f"{title} schema_version must be 1")
    _validate_text(document.get("title"), f"{title}.title")
    if document.get("repository") != "mrnicholasbcarter-code/verdict-core":
        raise ValueError(f"{title}.repository is incorrect")
    commit = document.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"{title}.commit must be a full lowercase git SHA")
    frozen = _parse_date(document.get("claim_freeze_date"), f"{title}.claim_freeze_date")
    review_after = _parse_date(document.get("review_after"), f"{title}.review_after")
    if review_after <= frozen:
        raise ValueError(f"{title}.review_after must be after claim_freeze_date")
    if review_after < date.today():
        raise ValueError(f"{title}.review_after is stale")
    return frozen, review_after


def _validate_matrix(document: dict[str, Any]) -> int:
    frozen, _ = _validate_common(document, "proof matrix")
    vocabulary = document.get("status_vocabulary")
    if not isinstance(vocabulary, list) or not all(isinstance(item, str) for item in vocabulary):
        raise ValueError("proof matrix.status_vocabulary must contain strings")
    if vocabulary != sorted(vocabulary):
        raise ValueError("proof matrix.status_vocabulary must be sorted")
    if set(vocabulary) != _ALLOWED_MATRIX_STATUSES:
        raise ValueError("proof matrix.status_vocabulary is incomplete")
    rows = document.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("proof matrix.rows must be non-empty")
    ids: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"proof matrix row {index} must be an object")
        row_id = row.get("id")
        if not isinstance(row_id, str) or row_id in ids:
            raise ValueError(f"proof matrix row {index} has a duplicate or invalid id")
        ids.add(row_id)
        if row.get("status") not in _ALLOWED_MATRIX_STATUSES:
            raise ValueError(f"proof matrix {row_id} has an invalid status")
        for field in ("area", "requirement", "verification", "public_wording", "gap"):
            _validate_text(row.get(field), f"proof matrix {row_id}.{field}")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"proof matrix {row_id}.evidence must be non-empty")
        for evidence_index, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValueError(f"proof matrix {row_id} evidence {evidence_index} is invalid")
            _validate_path(item.get("path"), field=f"proof matrix {row_id}.evidence.path")
            _validate_text(item.get("locator"), f"proof matrix {row_id}.evidence.locator")
            _validate_text(item.get("kind"), f"proof matrix {row_id}.evidence.kind")
        if row["status"] in {"blocked", "partial", "not_started"} and not row["gap"].strip():
            raise ValueError(f"proof matrix {row_id} needs a gap explanation")
        for field in ("requirement", "verification", "public_wording", "gap"):
            _validate_text(row[field], f"proof matrix {row_id}.{field}")
    if frozen > date.today():
        raise ValueError("proof matrix claim_freeze_date cannot be in the future")
    return len(rows)


def _validate_ledger(document: dict[str, Any], matrix_path: Path) -> int:
    frozen, _ = _validate_common(document, "claims ledger")
    statuses = document.get("statuses")
    if not isinstance(statuses, list) or not all(isinstance(item, str) for item in statuses):
        raise ValueError("claims ledger.statuses must contain strings")
    if statuses != sorted(statuses):
        raise ValueError("claims ledger.statuses is incomplete or unsorted")
    if set(statuses) != _ALLOWED_LEDGER_STATUSES:
        raise ValueError("claims ledger.statuses is incomplete or unsorted")
    _validate_text(document.get("public_evidence_rule"), "claims ledger.public_evidence_rule")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("claims ledger.entries must be non-empty")
    ids: set[str] = set()
    matrix = _load(matrix_path)
    matrix_rows = {row["id"] for row in matrix["rows"] if isinstance(row, dict)}
    matrix_paths = {
        item["path"]
        for row in matrix["rows"]
        for item in row["evidence"]
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"claims ledger entry {index} must be an object")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or entry_id in ids:
            raise ValueError(f"claims ledger entry {index} has a duplicate or invalid id")
        ids.add(entry_id)
        if entry.get("matrix_row") not in matrix_rows:
            raise ValueError(f"claims ledger {entry_id}.matrix_row is not in proof matrix")
        if entry.get("status") not in _ALLOWED_LEDGER_STATUSES:
            raise ValueError(f"claims ledger {entry_id} has an invalid status")
        for field in (
            "claim",
            "allowed_wording",
            "metric_definition",
            "authorship",
            "likely_objection",
            "falsification_test",
            "missing_evidence",
            "downgrade_wording",
        ):
            _validate_text(entry.get(field), f"claims ledger {entry_id}.{field}")
        evidence = entry.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError(f"claims ledger {entry_id}.evidence must be non-empty")
        for path in evidence:
            _validate_path(path, field=f"claims ledger {entry_id}.evidence")
            if path not in matrix_paths:
                raise ValueError(
                    f"claims ledger {entry_id} evidence is absent from proof matrix: {path}"
                )
        if not isinstance(entry.get("public"), bool):
            raise ValueError(f"claims ledger {entry_id}.public must be explicit")
        if entry.get("visibility") not in {"public", "redacted", "private"}:
            raise ValueError(f"claims ledger {entry_id}.visibility is invalid")
        _validate_text(
            entry.get("redacted_substitute"), f"claims ledger {entry_id}.redacted_substitute"
        )
        if entry.get("confidence") not in {"high", "medium", "low"}:
            raise ValueError(f"claims ledger {entry_id}.confidence is invalid")
        review_after = _parse_date(
            entry.get("review_after"), f"claims ledger {entry_id}.review_after"
        )
        if review_after <= frozen:
            raise ValueError(f"claims ledger {entry_id}.review_after is stale")
        if (
            entry["status"] in {"unsupported", "self_reported", "aspiration"}
            and entry["allowed_wording"] == entry["claim"]
        ):
            raise ValueError(f"claims ledger {entry_id} must downgrade unsupported wording")
    if frozen > date.today():
        raise ValueError("claims ledger claim_freeze_date cannot be in the future")
    return len(entries)


def validate(matrix_path: Path = MATRIX_PATH, ledger_path: Path = LEDGER_PATH) -> tuple[int, int]:
    """Validate both public ledgers and return their row counts."""

    matrix = _load(matrix_path)
    ledger = _load(ledger_path)
    return _validate_matrix(matrix), _validate_ledger(ledger, matrix_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=MATRIX_PATH)
    parser.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    args = parser.parse_args(argv)
    try:
        matrix_rows, ledger_entries = validate(args.matrix, args.ledger)
    except ValueError as exc:
        parser.error(str(exc))
    print(
        f"verified proof matrix ({matrix_rows} rows) and claims ledger ({ledger_entries} entries)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
