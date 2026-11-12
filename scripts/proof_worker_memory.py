#!/usr/bin/env python3
"""Proof: a worker brief carries cited memory, or an explicit recall status.

Exercises the production autodev worker-brief entrypoint
(``verdict.autodev_run.compile_unit_context``) against a temporary ai-memory
plane. It writes one verified decision, compiles the brief the worker receives
before any model call, and records the chain from query to retrieved source id
to the exact line emitted in the brief. It also records that a healthy search
with no relevant fact is reported differently from a missing plane.

No model call, no network, no write to the operator's real ~/.verdict. The
plane is a temp file this script creates and removes.

    python scripts/proof_worker_memory.py --output <dir>

Prints a JSON receipt and exits 0 when the chain holds, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path


def _sha256(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def prove(output_dir: Path) -> dict[str, object]:
    from verdict.autodev_run import compile_unit_context
    from verdict.context_intelligence import recall_memory_for_task
    from verdict.memory_gate import MemoryGate, MemoryWriteRequest
    from verdict.memory_plane import MemoryPlane
    from verdict.work_unit import WorkUnit

    output_dir.mkdir(parents=True, exist_ok=True)
    objective = "Update compute_widget without changing its contract"
    fact = "compute_widget keeps integer input; ADR-7."
    with tempfile.TemporaryDirectory(prefix="verdict-memory-proof-") as tmp:
        root = Path(tmp)
        plane_path = root / "memory.db"
        with MemoryPlane(plane_path) as plane:
            result = MemoryGate(plane).write(
                MemoryWriteRequest(
                    "decisions",
                    "widget-contract",
                    fact,
                    "verdict-core",
                    provenance={"source": "proof_worker_memory"},
                )
            )
            if not result.allowed or result.record is None:
                raise SystemExit(f"fixture write refused: {result.reason}")
            record_id = result.record.record_id

        recall = recall_memory_for_task(objective, plane_path=plane_path)
        import os

        os.environ["VERDICT_MEMORY_PLANE_PATH"] = str(plane_path)
        try:
            pack = compile_unit_context(
                WorkUnit(
                    unit_id="widget",
                    objective=objective,
                    owned_files=("pkg/widget.py",),
                    verification_command=("python", "-m", "pytest", "tests/test_widget.py"),
                ),
                root,
            )
        finally:
            os.environ.pop("VERDICT_MEMORY_PLANE_PATH", None)
        brief = "\n".join(unit.content for unit in pack.units)
        brief_path = output_dir / "worker-brief.txt"
        brief_path.write_text(brief, encoding="utf-8")

        cited = [item for item in recall.citations if item.record_id == record_id]
        chain_ok = bool(cited) and fact in brief and record_id in brief

        # A healthy plane with nothing relevant is not the same as no plane.
        empty = recall_memory_for_task("zzznomatch", plane_path=plane_path)
        missing = recall_memory_for_task(objective, plane_path=root / "absent.db")
        distinct = empty.status == "empty" and missing.status == "unavailable"

    receipt = {
        "schema": "verdict.worker-memory-proof/v1",
        "query": objective,
        "retrieved_source_id": record_id,
        "source_revision": cited[0].source_revision if cited else None,
        "source_revision_expected": _sha256(fact),
        "brief_path": str(brief_path),
        "brief_digest": _sha256(brief),
        "chain_ok": chain_ok,
        "empty_versus_unavailable_distinct": distinct,
        "passed": chain_ok
        and distinct
        and bool(cited)
        and cited[0].source_revision == _sha256(fact),
    }
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8"
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = prove(args.output)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
