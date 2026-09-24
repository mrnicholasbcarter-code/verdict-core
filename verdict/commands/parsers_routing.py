"""Argument registration for Verdict routing command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    route_p = subparsers.add_parser("route", help="Route a single prompt/task")
    route_p.add_argument("task", help="Task description or prompt text")
    route_p.add_argument("--terse", action="store_true", help="Output ONLY the target model string")
    route_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    route_p.add_argument(
        "--allow-offline",
        action="store_true",
        help=(
            "Decide from the static catalog only — no network discovery or probes. "
            "Does not enable the BOD-127 legacy selector escape (use --allow-legacy-selector)."
        ),
    )
    route_p.add_argument(
        "--allow-legacy-selector",
        action="store_true",
        help=(
            "BOD-127 migration escape: allow pre-BOD-104 selectors. "
            "Required explicitly; --allow-offline alone never enables this. "
            "Production serve must omit this and supply execution_path_decision."
        ),
    )

    compare_p = subparsers.add_parser(
        "compare", help="Compare a DIRECT frontier call vs the Verdict route for one task"
    )
    compare_p.add_argument("task", help="Task description or prompt text")
    compare_p.add_argument(
        "--criticality", default="medium", choices=["critical", "high", "medium", "low"]
    )
    compare_p.add_argument(
        "--allow-offline",
        action="store_true",
        help="Decide from the static catalog only — no network discovery or probes",
    )

    autodev_p = subparsers.add_parser(
        "autodev", help="Decompose an objective, execute each unit on a cheap route, and verify"
    )
    autodev_p.add_argument("--objective", help="What the run must accomplish")
    autodev_p.add_argument("--repo", default=".", help="Repository to work in (default: .)")
    autodev_p.add_argument(
        "--orchestrator-model",
        default=None,
        help="Optional model assertion; route is supplied by the BOD-104 ExecutionPathDecision",
    )
    autodev_p.add_argument(
        "--executor-model",
        default=None,
        help="Optional exact model assertion; the BOD-104 decision supplies the worker route",
    )
    autodev_p.add_argument(
        "--base-url",
        default=None,
        help="Optional gateway assertion; BOD-104 decision supplies the route",
    )
    autodev_p.add_argument("--json", action="store_true", help="Emit the machine-readable report")
    autodev_p.add_argument(
        "--allow-live",
        action="store_true",
        help="Consent to live model calls and working-tree edits",
    )
    autodev_p.add_argument(
        "--no-mechanical",
        action="store_true",
        help="Disable the zero-token deterministic tier and send every unit to a model",
    )
    autodev_p.add_argument(
        "--dry-run", action="store_true", help="Show the plan and its cost without executing"
    )
    autodev_p.add_argument(
        "--execution-path-request",
        default=None,
        help=(
            "public execution-path request JSON; the in-process optimizer "
            "decision is the launch authority (BOD-104)"
        ),
    )
    packet_p = autodev_p.add_subparsers(dest="autodev_action")
    packet_root = packet_p.add_parser("packet", help="Portable packet operations")
    packet_actions = packet_root.add_subparsers(dest="packet_action", required=True)
    for action in ("create", "inspect", "validate", "resume", "execute"):
        action_p = packet_actions.add_parser(action)
        action_p.add_argument("--packet", required=True)
        action_p.add_argument("--json", action="store_true")
        if action == "create":
            action_p.add_argument("--from", dest="source_path", required=True)
        if action == "resume":
            action_p.add_argument("--model", required=True)
        if action == "execute":
            action_p.add_argument(
                "--execution-path-request",
                default=None,
                help=(
                    "public execution-path request JSON; the in-process optimizer "
                    "decision is the launch authority (BOD-104)"
                ),
            )
            action_p.add_argument("--repo", required=True)
            action_p.add_argument(
                "--allow-live",
                action="store_true",
                help="consent: executes through the gateway and edits the working tree",
            )
            action_p.add_argument(
                "--prefer-non-primary",
                action="store_true",
                help="first attempt must use a concrete non-primary admitted route",
            )
            action_p.add_argument(
                "--primary-fallback",
                default=None,
                help="concrete route that currently occupies the primary-subscription role",
            )
            action_p.add_argument(
                "--base-url",
                dest="packet_base_url",
                default=None,
                help="override gateway family base URL for this packet run",
            )
            action_p.add_argument(
                "--canary",
                dest="canary_path",
                default=None,
                help="JSON canary state; chosen applies only among admitted_ids",
            )
            action_p.add_argument(
                "--delegation",
                choices=["legwork", "decision"],
                default=None,
                help=(
                    "required classification for this unit; a decision must also "
                    "carry --undelegable-reason"
                ),
            )
            action_p.add_argument(
                "--undelegable-reason",
                dest="undelegable_reason",
                default=None,
                help="capability that makes a --delegation decision unable to run non-primary",
            )
    shadow_p = packet_actions.add_parser(
        "shadow", help="Dump advisory shadow-learning JSON without calling eligibility"
    )
    shadow_p.add_argument("--episodes", required=True, help="JSON list of trusted episodes")
    shadow_p.add_argument("--json", action="store_true")
    canary_p = packet_actions.add_parser(
        "canary", help="Dump explicit bounded canary choice or rollback without eligibility"
    )
    canary_p.add_argument("--episodes", help="JSON list of trusted episodes")
    canary_p.add_argument("--admitted", help="JSON list of already-admitted identities")
    canary_p.add_argument("--rollback", help="JSON canary state to restore baseline")
    canary_p.add_argument("--json", action="store_true")
    compare_p = packet_actions.add_parser("compare", help="Compare two family-run JSON objects")
    compare_p.add_argument("--packet", required=True)
    compare_p.add_argument("--json", action="store_true")
    compare_p.add_argument("--a", dest="family_a_path", required=True)
    compare_p.add_argument("--b", dest="family_b_path", required=True)
