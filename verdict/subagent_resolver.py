"""
CLI/Resolver for pi-subagents dynamic model selection via OmniRoute/Verdict.

This module provides a simple CLI that pi-subagents can call to resolve
`omniroute/auto-<role>` model IDs to actual model IDs at launch time.

Usage:
    python -m verdict.subagent_resolver --role worker
    python -m verdict.subagent_resolver --role reviewer --diversity-from worker
    python -m verdict.subagent_resolver --role oracle --protected

Environment:
    OMNIROUTE_BASE_URL - OmniRoute API base URL (default: http://127.0.0.1:20132/v1)
    OMNIROUTE_API_KEY - API key for OmniRoute
    LLMGATE_AVAILABILITY_PROFILE - "production" to enable live probes
    LLMGATE_PROBE_BASE_URL - Probe endpoint for production
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from verdict.execution_path import ExecutionPathDecision, ExecutionPathError
from verdict.serve_path import require_serve_path_decision


def resolve_subagent_model(
    role: str,
    *,
    execution_path_decision: ExecutionPathDecision | None = None,
    protected: bool = False,
    dev_mode: bool = True,
    diversity_from: list[str] | None = None,
    json_output: bool = False,
) -> dict[str, Any] | None:
    """Resolve a role to the already-authorized BOD-104 concrete route.

    ``role`` remains a caller label for compatibility. It is not a selector:
    automatic subagent launches must receive an in-process
    :class:`ExecutionPathDecision` and use its exact route unchanged.
    """
    del protected, dev_mode, diversity_from
    try:
        decision = require_serve_path_decision(execution_path_decision, surface="subagent resolver")
        route = decision.selected_route
        if route is None:
            raise ExecutionPathError("subagent resolver: decision has no concrete route")
        result: dict[str, Any] = {
            "model_id": route.model,
            "provider": route.provider,
            "gateway": route.gateway,
            "route_id": route.route_id,
            "capability_tier": route.capability_tier,
            "role": role,
            "selected_strategy": decision.selected_strategy,
            "decision_digest": decision.decision_digest,
            "strategy_authority": "verdict.execution_path.optimize_execution_path",
        }
        if json_output:
            print(json.dumps(result, indent=2))
        return result
    except (ExecutionPathError, TypeError, ValueError) as exc:
        result = {"error": str(exc), "role": role}
        if json_output:
            print(json.dumps(result, indent=2))
        return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve pi-subagents role to OmniRoute/Verdict model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Roles: scout, worker, reviewer, oracle, planner, researcher, context-builder, delegate

Examples:
  %(prog)s --role worker
  %(prog)s --role reviewer --diversity-from worker
  %(prog)s --role oracle --protected --json
        """,
    )
    parser.add_argument("--role", required=True, help="Subagent role to resolve")
    parser.add_argument("--protected", action="store_true", help="Fail-closed for protected work")
    parser.add_argument(
        "--no-dev-mode",
        dest="dev_mode",
        action="store_false",
        help="Disable dev mode (strict eligibility)",
    )
    parser.add_argument(
        "--diversity-from", action="append", default=[], help="Model IDs to exclude for diversity"
    )
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")

    args = parser.parse_args()

    result = resolve_subagent_model(
        role=args.role,
        protected=args.protected,
        dev_mode=args.dev_mode,
        diversity_from=args.diversity_from or None,
        json_output=args.json,
    )

    if result and "error" in result:
        if not args.json:
            print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    if not args.json and result:
        print(f"Resolved: {result['model_id']} ({result['provider']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
