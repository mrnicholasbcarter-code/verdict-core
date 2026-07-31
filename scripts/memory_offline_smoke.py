"""Produce a deterministic, redacted MemoryPlane offline smoke report."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane


def _redacted_record_shape(record: dict[str, Any]) -> dict[str, Any]:
    """Expose schema/provenance shape without exporting memory content."""

    provenance = record.get("provenance")
    return {
        "fields": sorted(record),
        "schema_version": record.get("schema_version"),
        "content_hash_present": isinstance(record.get("content_hash"), str),
        "provenance_fields": sorted(provenance) if isinstance(provenance, dict) else [],
        "content_field_present": "content" in record,
        "content_values_redacted": True,
        "metadata_field_present": "metadata" in record,
        "metadata_values_redacted": True,
    }


def run() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="verdict-memory-smoke-") as directory:
        path = Path(directory) / "memory.db"
        with MemoryPlane(path) as plane:
            gate = MemoryGate(plane)
            first = gate.write(
                MemoryWriteRequest(
                    namespace="patterns",
                    key="offline-routing",
                    value={"outcome": "safe", "api_key": "not-persisted"},
                    authority="verdict-core",
                    provenance={"source": "offline-fixture", "commit": "fixture"},
                    scope="smoke",
                )
            )
            duplicate = gate.write(
                MemoryWriteRequest(
                    namespace="patterns",
                    key="offline-routing",
                    value={"outcome": "safe", "api_key": "not-persisted"},
                    authority="verdict-core",
                    provenance={"source": "offline-fixture", "commit": "fixture"},
                    scope="smoke",
                )
            )
            search = [record.record_id for record in plane.search("safe", scope="smoke")]
            export_a = plane.export_records(scope="smoke")
            export_b = plane.export_records(scope="smoke")
            schema_shapes = sorted(
                (_redacted_record_shape(record) for record in export_a),
                key=lambda shape: json.dumps(shape, sort_keys=True, separators=(",", ":")),
            )
            return {
                "schema_version": 1,
                "backend": plane.health()["backend"],
                "status": "ready" if first.allowed and duplicate.allowed else "blocked",
                "network": "disabled",
                "provider": "not_required",
                "records": len(export_a),
                "search_hit_count": len(search),
                "deterministic_export": export_a == export_b,
                "redaction_proven": "not-persisted" not in json.dumps(export_a, sort_keys=True),
                "gate_event_count": len(gate.get_write_history()),
                "record_schema": schema_shapes,
                "provenance_shape_digest": hashlib.sha256(
                    json.dumps(schema_shapes, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = json.dumps(run(), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
