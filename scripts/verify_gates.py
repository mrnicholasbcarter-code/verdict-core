#!/usr/bin/env python3
"""Validate a complete, local release acceptance-gate evidence report.

The verifier is intentionally stdlib-only and fail-closed.  It validates the
report structure and confirms that every referenced evidence file exists below
the caller-supplied evidence directory.  It never turns a missing artifact or
an unknown gate into a pass.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
REPOSITORY = "mrnicholasbcarter-code/verdict-core"
REPORT_NAME = "gates_status.json"
STATUSES = frozenset({"PASS", "FAIL", "BLOCKED"})

# Keep this list aligned with the G1-G7 IDs in ACCEPTANCE_GATES.md.  The
# verifier deliberately requires the complete set rather than accepting a
# partial report that could be mistaken for a release decision.
GATE_IDS = tuple(
    gate_id
    for category, count in ((1, 5), (2, 4), (3, 4), (4, 4), (5, 4), (6, 4), (7, 4))
    for gate_id in (f"G{category}.{criterion}" for criterion in range(1, count + 1))
)


class GateValidationError(ValueError):
    """Raised when a report or its evidence is unsafe or incomplete."""


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GateValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise GateValidationError(f"non-standard JSON constant: {value}")


def _load_canonical_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateValidationError(f"cannot read {REPORT_NAME}") from exc
    if path.is_symlink():
        raise GateValidationError(f"{REPORT_NAME} must not be a symlink")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, GateValidationError) as exc:
        raise GateValidationError(f"{REPORT_NAME} is malformed JSON") from exc
    if not isinstance(value, dict):
        raise GateValidationError(f"{REPORT_NAME} must contain a JSON object")
    if raw != _canonical_json(value):
        raise GateValidationError(f"{REPORT_NAME} is not canonical JSON")
    return value


def _validate_evidence_root(value: Path) -> Path:
    root = Path(value)
    if root.is_symlink() or not root.is_dir():
        raise GateValidationError("evidence directory must be a real directory")
    return root


def _validate_evidence_path(root: Path, value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise GateValidationError("evidence paths must be repository-relative POSIX paths")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
        or value == REPORT_NAME
    ):
        raise GateValidationError(f"unsafe evidence path: {value!r}")

    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise GateValidationError(f"evidence file is missing: {value}") from exc
        if stat.S_ISLNK(mode):
            raise GateValidationError(f"evidence path contains a symlink: {value}")
    if not stat.S_ISREG(mode):
        raise GateValidationError(f"evidence path is not a regular file: {value}")
    return value


def _validate_report(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    expected_fields = {"schema_version", "repository", "commit", "gates"}
    if set(report) != expected_fields:
        raise GateValidationError(
            "report fields must be exactly schema_version, repository, commit, gates"
        )
    if report.get("schema_version") != SCHEMA_VERSION:
        raise GateValidationError("unsupported report schema_version")
    if report.get("repository") != REPOSITORY:
        raise GateValidationError("report repository does not match this project")
    commit = report.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        raise GateValidationError("report commit must be a full lowercase git SHA")

    gates = report.get("gates")
    if not isinstance(gates, list) or [
        item.get("id") for item in gates if isinstance(item, dict)
    ] != list(GATE_IDS):
        raise GateValidationError("report must contain every gate ID in documented order")

    counts = {status: 0 for status in STATUSES}
    for index, item in enumerate(gates):
        if not isinstance(item, dict) or set(item) != {"id", "status", "evidence"}:
            raise GateValidationError(f"gate {index} has unexpected or missing fields")
        gate_id = item["id"]
        status = item["status"]
        evidence = item["evidence"]
        if not isinstance(gate_id, str) or gate_id != GATE_IDS[index]:
            raise GateValidationError(f"gate {index} has an invalid ID")
        if not isinstance(status, str) or status not in STATUSES:
            raise GateValidationError(f"{gate_id} has an invalid status")
        if not isinstance(evidence, list) or not evidence:
            raise GateValidationError(f"{gate_id} must cite at least one evidence file")
        seen: set[str] = set()
        for evidence_path in evidence:
            normalized = _validate_evidence_path(root, evidence_path)
            if normalized in seen:
                raise GateValidationError(f"{gate_id} cites duplicate evidence: {normalized}")
            seen.add(normalized)
        counts[status] += 1

    return {
        "valid": True,
        "schema_version": SCHEMA_VERSION,
        "repository": REPOSITORY,
        "commit": commit,
        "gate_count": len(gates),
        "passed": counts["PASS"],
        "failed": counts["FAIL"],
        "blocked": counts["BLOCKED"],
        "all_passed": counts["PASS"] == len(GATE_IDS),
    }


def validate_gates(evidence_dir: Path) -> dict[str, Any]:
    """Validate ``gates_status.json`` and all referenced evidence files."""

    root = _validate_evidence_root(evidence_dir)
    report = _load_canonical_json(root / REPORT_NAME)
    return _validate_report(root, report)


def _emit(summary: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    else:
        state = "PASS"
        if summary["failed"]:
            state = "FAIL"
        elif summary["blocked"]:
            state = "BLOCKED"
        print(
            f"{state}: validated {summary['gate_count']} gates "
            f"({summary['passed']} pass, {summary['failed']} fail, {summary['blocked']} blocked)"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        summary = validate_gates(args.evidence_dir)
    except GateValidationError as exc:
        if args.as_json:
            print(json.dumps({"valid": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    _emit(summary, as_json=args.as_json)
    return 0 if summary["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
