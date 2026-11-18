"""Argument registration for `verdict ladder` CLI command (T3: Slice 1)."""

from __future__ import annotations

from typing import Any

from verdict.actions.base import ActionResult, ActionSpec
from verdict.actions.registry import register as _register_action


def register(subparsers: Any) -> None:
    """Register the ladder subcommand."""
    ladder_p = subparsers.add_parser(
        "ladder", help="Build and display the role-based model selection ladder (read-only)"
    )
    ladder_p.add_argument(
        "--role",
        required=True,
        choices=["planner"],  # Only planner enabled in Slice 1
        help="Role to build ladder for (only planner in Slice 1)",
    )
    ladder_p.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON (LadderResult + shadow event)",
    )
    ladder_p.add_argument(
        "--inventory-fixture",
        type=str,
        default=None,
        help="Path to JSON fixture file for inventory (sets metadata_fixture=true, live_proof=false)",
    )
    ladder_p.add_argument(
        "--node-id", type=str, default="", help="Node ID for shadow event correlation"
    )
    ladder_p.add_argument(
        "--run-id", type=str, default="", help="Run ID for shadow event correlation"
    )
    ladder_p.add_argument(
        "--correlation-id",
        type=str,
        default="",
        help="Correlation ID for shadow event (joins later dispatch outcome)",
    )
    ladder_p.add_argument(
        "--mode",
        type=str,
        default="shadow",
        choices=["shadow", "off"],
        help="Selection mode (shadow=calculate dynamic but use off result; off=legacy only)",
    )

    # Register ladder as a read-only action so the action-layer exclusion
    # audit classifies the CLI leaf.
    _register_action(
        ActionSpec(
            "ladder",
            "routing",
            "read",
            "Build and display the role-based model selection ladder",
            "Routing",
        ),
        lambda **kwargs: ActionResult(
            data={"ok": True}, ok=True, exit_code=0
        ),  # handler is in dispatch._cmd_ladder
    )
