"""Argument registration for Verdict setup command family."""

from __future__ import annotations

from typing import Any


def register(subparsers: Any) -> None:
    setup_cli_p = subparsers.add_parser("setup", help="Plan or apply the interactive setup wizard")
    setup_cli_p.add_argument(
        "setup_action",
        nargs="?",
        choices=["plan", "intelligence", "gateways", "harnesses", "credentials"],
        help="Read-only setup operation or capability scope",
    )
    setup_cli_p.add_argument(
        "--dry-run", action="store_true", help="Build a mutation-free setup plan"
    )
    setup_cli_p.add_argument(
        "--plan", action="store_true", help="Mutation-free capability bootstrap plan"
    )
    setup_cli_p.add_argument(
        "--recommended",
        action="store_true",
        help="Show recommended enrichment set (still mutation-free without --apply)",
    )
    setup_cli_p.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    setup_cli_p.add_argument(
        "--non-interactive",
        action="store_true",
        help="Headless/CI mode: no prompts; APPLY only via --allow ( --yes is not enough)",
    )
    setup_cli_p.add_argument(
        "--apply",
        action="store_true",
        help=(
            "Apply authorized bootstrap actions "
            "(interactive: --yes; non-interactive: --allow provider ids)"
        ),
    )
    setup_cli_p.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Interactive final consent for the shown APPLY plan; "
            "ignored as blanket auth under --non-interactive (use --allow)"
        ),
    )
    setup_cli_p.add_argument(
        "--allow",
        dest="allowlist",
        action="append",
        default=[],
        help=(
            "Explicit allowlist provider id (repeatable), e.g. gateway.omniroute; "
            "required for non-interactive APPLY"
        ),
    )
    setup_cli_p.add_argument(
        "--rollback",
        action="store_true",
        help="Roll back Verdict-owned bootstrap APPLY records (ownership/backups; no TUI)",
    )
    setup_cli_p.add_argument(
        "--rollback-action",
        dest="rollback_actions",
        action="append",
        default=[],
        help="Limit rollback to a managed action_id (repeatable)",
    )
    setup_cli_p.add_argument(
        "--state-dir",
        default=None,
        help="Bootstrap ownership state directory (default: ~/.verdict/bootstrap)",
    )
