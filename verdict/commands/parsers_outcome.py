"""Argument registration for the Verdict outcome-attempt commands (BOD-353).

Every model attempt (autodev, route, orchestrate or a Prime controller child)
funnels through ``verdict.outcome_records.build_attempt_record`` /
``write_attempt_record``. This parser exposes that writer as
``verdict outcome record`` plus a one-shot importer for the legacy
hand-written ``work/model-decisions.jsonl`` log.
"""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    outcome_p = subparsers.add_parser(
        "outcome", help="Record and import classified model-attempt outcomes (BOD-353)"
    )
    outcome_sub = outcome_p.add_subparsers(dest="outcome_command", required=True)

    record_p = outcome_sub.add_parser("record", help="Record one classified model attempt")
    record_p.add_argument("--task-kind", required=True, help="e.g. implement, review, research")
    record_p.add_argument("--harness", required=True, help="e.g. prime, verdict-autodev, route")
    record_p.add_argument("--model", required=True, help="Provider/model id (any gateway prefix)")
    record_p.add_argument(
        "--outcome", required=True, choices=["accepted", "failed", "partial", "abandoned"]
    )
    record_p.add_argument(
        "--failure-class",
        required=True,
        help="One of the fixed BOD-353 failure classes (e.g. success, quota_429, timeout)",
    )
    record_p.add_argument("--source", required=True, help="Caller identity (e.g. cli, autodev)")
    record_p.add_argument("--effort", default=None)
    record_p.add_argument("--duration-s", type=float, default=None)
    record_p.add_argument("--tokens-in", type=int, default=None)
    record_p.add_argument("--tokens-out", type=int, default=None)
    record_p.add_argument("--cost-usd", type=float, default=None)
    record_p.add_argument("--story-id", default=None)
    record_p.add_argument("--store-dir", default=None, help="Default: $VERDICT_HOME")
    record_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    import_p = outcome_sub.add_parser(
        "import", help="Import legacy work/model-decisions.jsonl rows (best-effort mapping)"
    )
    import_p.add_argument("jsonl_path", help="Path to the legacy model-decisions.jsonl file")
    import_p.add_argument(
        "--source", default="model-decisions-import", help="Recorded source for imported rows"
    )
    import_p.add_argument("--store-dir", default=None, help="Default: $VERDICT_HOME")
    import_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
