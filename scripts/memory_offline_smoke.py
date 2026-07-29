"""Produce a deterministic, redacted MemoryPlane offline smoke report."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane


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
            return {
                "status": "ready" if first.allowed and duplicate.allowed else "blocked",
                "network": "disabled",
                "provider": "not_required",
                "records": len(export_a),
                "search_record_ids": search,
                "deterministic_export": export_a == export_b,
                "redaction_proven": "not-persisted" not in json.dumps(export_a, sort_keys=True),
                "gate_event_count": len(gate.get_write_history()),
            }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(run(), indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    print(report, end="")


if __name__ == "__main__":
    main()
