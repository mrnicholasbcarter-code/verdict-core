"""Argument registration for Verdict models command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    run_p = subparsers.add_parser("run", help="Route a single prompt/task (alias of route)")
    run_p.add_argument("task", help="Task description or prompt text")
    run_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    run_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )

    plan_p = subparsers.add_parser("plan", help="Print a mutation-free setup plan")
    plan_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    choose_p = subparsers.add_parser(
        "choose", help="Choose an eligible execution target for Prime dispatch"
    )
    choose_p.add_argument(
        "--task-class",
        required=True,
        dest="task_class",
        help="Task class (implementation, architecture, ...)",
    )
    choose_p.add_argument(
        "--requires", default="", help="Comma-separated required capabilities (example: tools,code)"
    )
    choose_p.add_argument(
        "--model",
        default=None,
        help="Explicit model identity; eligible selection wins, ineligible fails closed",
    )
    choose_p.add_argument(
        "--candidates-json",
        default=None,
        help="JSON file of CandidateEvidence fixtures (required for P0)",
    )
    choose_p.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON receipt"
    )

    models_p = subparsers.add_parser(
        "models", help="List the qualified model catalog used for routing and simulation"
    )
    models_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    inspect_p = subparsers.add_parser("inspect", help="Inspect one model's catalog record")
    inspect_p.add_argument("model_id", help="Model ID to inspect")
    inspect_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    receipt_p = subparsers.add_parser(
        "receipt", help="Inspect durable RoutingReceiptV1 records (BOD-144)"
    )
    receipt_sub = receipt_p.add_subparsers(dest="receipt_action", required=True)
    receipt_list_p = receipt_sub.add_parser("list", help="List routing receipts")
    receipt_list_p.add_argument("--scope", default=None, help="Optional receipt scope filter")
    receipt_list_p.add_argument(
        "--db",
        dest="db_path",
        default=None,
        help="ReceiptStore sqlite path (default: .verdict/receipts.db)",
    )
    receipt_list_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    receipt_show_p = receipt_sub.add_parser("show", help="Show one routing receipt")
    receipt_show_p.add_argument("receipt_id", nargs="?", default=None, help="Receipt id")
    receipt_show_p.add_argument("--attempt", dest="attempt_id", default=None, help="Attempt id")
    receipt_show_p.add_argument("--scope", default=None, help="Receipt scope")
    receipt_show_p.add_argument(
        "--db", dest="db_path", default=None, help="ReceiptStore sqlite path"
    )
    receipt_show_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    receipt_export_p = receipt_sub.add_parser("export", help="Export one routing receipt as JSON")
    receipt_export_p.add_argument("receipt_id", nargs="?", default=None, help="Receipt id")
    receipt_export_p.add_argument("--attempt", dest="attempt_id", default=None, help="Attempt id")
    receipt_export_p.add_argument("--scope", default=None, help="Receipt scope")
    receipt_export_p.add_argument(
        "--db", dest="db_path", default=None, help="ReceiptStore sqlite path"
    )

    replay_p = subparsers.add_parser("replay", help="Replay a recorded execution session")
    replay_p.add_argument("session_id", help="Session ID to replay")
    replay_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    failover_p = subparsers.add_parser(
        "failover-proof", help="Run the offline forced-failover and replay proof"
    )
    failover_p.add_argument(
        "--memory-path",
        default=".verdict-failover-memory.db",
        help="MemoryPlane database path for the replayable session",
    )
    failover_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    simulate_p = subparsers.add_parser(
        "simulate", help="Forecast tokens, cost, risk, and model before any paid call"
    )
    simulate_p.add_argument("task", help="Task description or prompt text")
    simulate_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    simulate_p.add_argument("--model", dest="model_override", default=None, help="Model override")
    simulate_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    subparsers.add_parser("cost-report", help="Estimate token cost from routing decision history")

    resume_p = subparsers.add_parser(
        "resume", help="Reconstruct durable story resume state (worktree + handoff + prompt)"
    )
    resume_p.add_argument("story", help="Linear story id (e.g. BOD-65)")
    resume_p.add_argument(
        "--with",
        dest="with_harness",
        choices=["claude", "codex", "cursor", "prime"],
        default=None,
        help="Optional harness launcher stub (records intent; does not exec yet)",
    )
    resume_p.add_argument(
        "--repo", default=".", help="Repository path used for worktree discovery (default: cwd)"
    )
    resume_p.add_argument(
        "--create",
        action="store_true",
        help="Create a story worktree when none exists (reattach-before-create still applies)",
    )
    resume_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    from verdict.orchestration import cli as _orchestration_cli

    _orchestration_cli.add_parsers(subparsers)
