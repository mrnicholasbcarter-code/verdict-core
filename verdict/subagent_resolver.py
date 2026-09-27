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
from pathlib import Path
from typing import Any

from verdict.execution_path import (
    ExecutionPathDecision,
    ExecutionPathError,
    ExecutionPathRequest,
    optimize_execution_path,
)
from verdict.serve_path import require_serve_path_decision


def public_execution_path_request(raw: Any, *, task: str) -> ExecutionPathRequest:
    """Validate public JSON and rebuild trusted request types.

    The ``api:`` plan/source labels are a normalized contract shared with the
    API boundary. Keeping them identical preserves cross-entry-point digests.
    """

    import hashlib
    import json
    from datetime import datetime, timezone
    from typing import cast

    from verdict.cost_ledger import PriceEvidenceInput
    from verdict.effective_capability import (
        AssistanceCost,
        AssistancePlan,
        DecompositionRequirement,
        ProvenanceClaim,
        TaskSlice,
        VerificationStrategy,
    )
    from verdict.execution_path import (
        EXECUTION_PATH_SCHEMA_VERSION,
        ExecutionPathError,
        ExecutionPathOffer,
    )
    from verdict.expected_cost import build_strategy_from_assistance
    from verdict.runtime_certification import CertificationState
    from verdict.session_economics import ConcreteRoute

    if not isinstance(raw, dict):
        raise ExecutionPathError("execution_path_request must be a JSON object")
    if raw.get("schema_version") != EXECUTION_PATH_SCHEMA_VERSION:
        raise ExecutionPathError(
            f"execution_path_request.schema_version must be {EXECUTION_PATH_SCHEMA_VERSION!r}"
        )
    trajectory_id = raw.get("trajectory_id")
    slice_id = raw.get("slice_id")
    candidates = raw.get("candidates")
    if not isinstance(trajectory_id, str) or not trajectory_id.strip():
        raise ExecutionPathError("execution_path_request.trajectory_id must be non-empty")
    if not isinstance(slice_id, str) or not slice_id.strip():
        raise ExecutionPathError("execution_path_request.slice_id must be non-empty")
    if not isinstance(candidates, list) or not candidates:
        raise ExecutionPathError("execution_path_request.candidates must be a non-empty array")
    acceptance = raw.get("acceptance_criteria", [])
    proof = raw.get("proof_criteria", [])
    if not isinstance(acceptance, list) or not all(isinstance(item, str) for item in acceptance):
        raise ExecutionPathError("execution_path_request.acceptance_criteria must be strings")
    if not isinstance(proof, list) or not proof or not all(isinstance(item, str) for item in proof):
        raise ExecutionPathError("execution_path_request.proof_criteria must be non-empty strings")
    task_slice = TaskSlice(
        slice_id=slice_id.strip(),
        objective=task,
        acceptance_criteria=tuple(acceptance),
        proof_criteria=tuple(proof),
    )
    now = datetime.now(timezone.utc)
    offers: list[ExecutionPathOffer] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}] must be an object"
            )
        required_strings = ("route_id", "gateway", "provider", "model")
        for field_name in required_strings:
            if not isinstance(candidate.get(field_name), str) or not candidate[field_name].strip():
                raise ExecutionPathError(
                    f"execution_path_request.candidates[{index}].{field_name} must be non-empty"
                )
        strategy = candidate.get("strategy", "direct_cheap")
        if not isinstance(strategy, str):
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}].strategy must be a string"
            )
        try:
            capability_tier = int(candidate.get("capability_tier", 2))
            execution_tokens = int(candidate.get("execution_tokens", 1))
            verification_tokens = int(candidate.get("verification_tokens", 1))
        except (TypeError, ValueError) as exc:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}] token/tier fields must be integers"
            ) from exc
        if execution_tokens < 1 or verification_tokens < 1:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}] execution and verification tokens must be positive"
            )
        certification = candidate.get("certification_state")
        try:
            certification_state = CertificationState(str(certification))
        except ValueError as exc:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}].certification_state is invalid"
            ) from exc
        route_id = candidate["route_id"].strip()
        evidence_payload = json.dumps(candidate, sort_keys=True, separators=(",", ":"))
        evidence_digest = "sha256:" + hashlib.sha256(evidence_payload.encode()).hexdigest()
        assistance = AssistanceCost(verification_tokens=verification_tokens)
        plan = AssistancePlan(
            plan_id=f"api:{trajectory_id}:{index}",
            candidate_id=route_id,
            task_slice=task_slice,
            required_intrinsic_capabilities=(),
            required_context_slots=(),
            required_context_evidence=(),
            required_tool_capabilities=(),
            selected_tool_surface=(),
            decomposition=DecompositionRequirement(required=False),
            verification=VerificationStrategy(
                kind="caller_proof_contract", proof_criteria=tuple(proof)
            ),
            assistance_cost=assistance,
            result="sufficient" if candidate.get("eligible") is True else "insufficient",
            reasons=("public_optimizer_contract",),
            intrinsic_sufficient=candidate.get("eligible") is True,
            assisted_sufficient=candidate.get("eligible") is True,
            assistance_delta=(),
            provenance=(
                ProvenanceClaim(
                    kind="optimizer_contract",
                    claim=route_id,
                    source="api:execution_path_request",
                    digest=evidence_digest,
                    observed_at=now.isoformat(),
                    freshness=str(candidate.get("certification_freshness", "unknown")),
                    fresh=str(candidate.get("certification_freshness", "unknown")) == "fresh",
                ),
            ),
            evidence_digest=evidence_digest,
        )
        is_free = candidate.get("is_free") is True
        raw_price = candidate.get("price")
        price = cast(PriceEvidenceInput, raw_price) if isinstance(raw_price, dict) else None
        if is_free and price is None:
            price = PriceEvidenceInput(
                input_usd_per_mtok="0",
                output_usd_per_mtok="0",
                observed_at=now.isoformat(),
                evidence_id=evidence_digest,
            )
        if price is None:
            raise ExecutionPathError(
                f"execution_path_request.candidates[{index}].price is required for non-free routes"
            )
        route = ConcreteRoute(
            route_id=route_id,
            gateway=candidate["gateway"].strip(),
            provider=candidate["provider"].strip(),
            model=candidate["model"].strip(),
            credential_pool=candidate.get("credential_pool"),
            capability_tier=capability_tier,
            eligible=candidate.get("eligible") is True,
            excluded=candidate.get("excluded") is True,
            exclusion_reason=candidate.get("exclusion_reason"),
        )
        expected = build_strategy_from_assistance(
            strategy_id=f"{strategy}:{route_id}",
            trajectory_id=trajectory_id.strip(),
            assistance=assistance,
            execution_tokens=execution_tokens,
            price=price,
            is_free=is_free,
            qualified=candidate.get("eligible") is True,
            free_first_preferred=True,
            now=now,
        )
        offers.append(
            ExecutionPathOffer(
                strategy=strategy,  # type: ignore[arg-type]
                route=route,
                assistance_plan=plan,
                expected_cost=expected,
                certification_state=certification_state,
                certification_freshness=str(candidate.get("certification_freshness", "unknown")),
                hard_excluded=candidate.get("excluded") is True,
                is_cheap=is_free or candidate.get("is_cheap") is True,
                is_paid=candidate.get("is_paid") is True,
                is_frontier=candidate.get("is_frontier") is True,
            )
        )
    return ExecutionPathRequest(
        task_slice=task_slice,
        trajectory_id=trajectory_id.strip(),
        offers=tuple(offers),
        assumptions=("public_json_validated_at_api_boundary",),
        now=now,
    )


def resolve_subagent_model(
    role: str,
    *,
    execution_path_decision: ExecutionPathDecision | None = None,
    protected: bool = False,
    dev_mode: bool = True,
    diversity_from: list[str] | None = None,
    json_output: bool = False,
) -> dict[str, Any] | None:
    """Resolve a role to the already-authorized execution-path authority concrete route.

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
    parser.add_argument(
        "--execution-path-request",
        default=None,
        help="public execution-path request JSON; the in-process optimizer decision is the launch authority (BOD-104)",
    )
    parser.add_argument(
        "--task", default=None, help="task objective bound to the request's task slice"
    )

    args = parser.parse_args()
    decision: ExecutionPathDecision | None = None
    if args.execution_path_request is not None:
        if args.task is None or not args.task.strip():
            parser.error("--execution-path-request requires --task")
        try:
            raw = json.loads(Path(args.execution_path_request).read_text(encoding="utf-8"))
            request = public_execution_path_request(raw, task=args.task)
            decision = optimize_execution_path(request)
        except (OSError, ValueError, ExecutionPathError) as exc:
            if args.json:
                print(json.dumps({"error": str(exc), "role": args.role}, indent=2))
            else:
                print(f"Error: {exc}", file=sys.stderr)
            return 1

    result = resolve_subagent_model(
        role=args.role,
        execution_path_decision=decision,
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
