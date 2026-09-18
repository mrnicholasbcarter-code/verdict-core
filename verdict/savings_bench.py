"""Paired frontier-direct vs Verdict savings bench (BOD-101 / BOD-114).

Talk track: "we measure". Cost and token usage come only from observed
OmniRoute response headers or matching receipt fields, bound to an execution
ID. Cache hits are labeled and never sold as model savings. Quality misses are
reported and withhold a savings claim. The Verdict arm must actually route
through the live chooser against a hydrated pack.

Two modes:

* **simulation-not-executed** (default, offline fixture): neither arm is
  executed. The report is explicitly labeled a simulation, every task is
  withheld with ``simulation_not_executed``, and ``savings_claimed`` is never
  true. Fixture identities are reported but flagged as unbound.
* **live-paired**: an ``execute_arm`` hook (see :mod:`verdict.savings_execution`
  and :mod:`verdict.savings_live`) runs both arms on the same input hash. A
  claim requires both execution IDs, matching input hashes, observed costs,
  provider-bound identities with an explained attempt chain, a hydrated pack,
  no cache hit, and quality evaluated from the produced output.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from verdict.classifier import classify
from verdict.context_hydrate import DEFAULT_CONTEXT_ROOTS
from verdict.fixture_paths import (
    default_fixture_path,
    resolve_fixture_path,
    resolve_fixture_workspace,
)
from verdict.free_tier_admit import OmniRouteAdmitSnapshot, snapshot_from_payloads
from verdict.intelligence import IntelligenceService
from verdict.metadata.records import (
    SOURCE_MODELS_DEV,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig
from verdict.pack_state import savings_unlocked
from verdict.savings_execution import (
    ARM_DIRECT,
    ARM_VERDICT,
    ArmExecution,
    ArmExecutor,
    ArmRequest,
    QualityEvaluator,
    QualityResult,
    canonical_input_hash,
    evaluate_output_against_checks,
    execution_evidence_gaps,
    explain_identity,
    validate_quality,
)

DEFAULT_SAVINGS_FIXTURE_PATH = default_fixture_path("benchmarks/fixtures/legit_paired_savings.json")
TALK_TRACK = "we measure"
SAVINGS_REPORT_SCHEMA_VERSION = "2"
MODE_SIMULATION = "simulation-not-executed"
MODE_LIVE_PAIRED = "live-paired"
WITHHOLD_SIMULATION = "simulation_not_executed"
COST_HEADER = "x-omniroute-response-cost"
TOKENS_IN_HEADER = "x-omniroute-tokens-in"
TOKENS_OUT_HEADER = "x-omniroute-tokens-out"
CACHE_HIT_HEADER = "x-omniroute-cache-hit"
MODEL_HEADER = "x-omniroute-model"
_FETCHED = "2026-09-18T18:00:00Z"
_NOW = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)
_KINDS = frozenset({"debug", "refactor_tests", "implement_from_ac"})


@dataclass(frozen=True)
class _CatalogIdentity:
    identity_id: str
    provider: str
    free: bool = False
    free_model_id: str | None = None
    tools: bool = True
    vision: bool = False
    context: int | None = None


# Representative catalog so the chooser actually chooses. None of these slugs
# is the measured identity — that must be specified on each arm.
_CATALOG: tuple[_CatalogIdentity, ...] = (
    _CatalogIdentity("opencode/hy3-free", "opencode", free=True, free_model_id="hy3-free"),
    _CatalogIdentity(
        "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
        "openrouter",
        free=True,
        free_model_id="nvidia/nemotron-3-nano-30b-a3b:free",
        tools=False,
    ),
    _CatalogIdentity("groq/llama-3.3-70b-versatile", "groq", context=131072),
    _CatalogIdentity("cx/gpt-5.6-sol", "cx", vision=True, context=200000),
    _CatalogIdentity("openai/gpt-5.5", "openai", vision=True, context=200000),
)


@dataclass(frozen=True)
class MeasuredCost:
    usd: float
    tokens_in: int
    tokens_out: int
    cache_hit: bool
    source: str


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fixture_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _headers(arm: Mapping[str, Any]) -> dict[str, str]:
    raw = arm.get("headers")
    if not isinstance(raw, dict):
        return {}
    return {str(key).lower(): str(value) for key, value in raw.items()}


def _receipt(arm: Mapping[str, Any]) -> dict[str, Any]:
    raw = arm.get("receipt")
    return dict(raw) if isinstance(raw, dict) else {}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def parse_measured_cost(arm: Mapping[str, Any]) -> MeasuredCost:
    """Read cost only from OmniRoute headers or matching receipt fields."""
    headers = _headers(arm)
    receipt = _receipt(arm)
    raw_cost = headers.get(COST_HEADER, receipt.get("response_cost_usd"))
    raw_in = headers.get(TOKENS_IN_HEADER, receipt.get("tokens_in"))
    raw_out = headers.get(TOKENS_OUT_HEADER, receipt.get("tokens_out"))
    if raw_cost is None or raw_in is None or raw_out is None:
        raise ValueError(
            "arm cost must come from X-OmniRoute-Response-Cost / Tokens-In/Out "
            "headers or receipt.response_cost_usd + tokens_in/tokens_out"
        )
    try:
        usd = float(raw_cost)
        tokens_in = int(raw_in)
        tokens_out = int(raw_out)
    except (TypeError, ValueError) as exc:
        raise ValueError("arm cost headers/receipt fields must be numeric") from exc
    if usd < 0 or tokens_in < 0 or tokens_out < 0:
        raise ValueError("arm cost fields must be non-negative")
    cache_hit = _truthy(headers.get(CACHE_HIT_HEADER, receipt.get("cache_hit", False)))
    source = "headers" if COST_HEADER in headers else "receipt"
    return MeasuredCost(
        usd=usd, tokens_in=tokens_in, tokens_out=tokens_out, cache_hit=cache_hit, source=source
    )


def _used_model(arm: Mapping[str, Any]) -> str:
    """Identity the arm's measured cost belongs to. Must be specified, never inferred."""
    headers = _headers(arm)
    for key in (MODEL_HEADER, "x-omniroute-completed-with"):
        stamped = headers.get(key)
        if isinstance(stamped, str) and stamped.strip():
            return stamped.strip()
    receipt = _receipt(arm)
    for key in ("completed_with", "model"):
        raw = arm.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        stamped = receipt.get(key)
        if isinstance(stamped, str) and stamped.strip():
            return stamped.strip()
    raise ValueError(
        "each arm must specify the model used via X-OmniRoute-Model, "
        "completed_with, or model — identities are not inferred"
    )


def _quality(arm: Mapping[str, Any]) -> dict[str, Any]:
    raw = arm.get("quality")
    if not isinstance(raw, dict):
        raise ValueError("each arm must include quality.passed")
    passed, misses = validate_quality(raw.get("passed"), raw.get("misses", []), where="quality")
    return {"passed": passed, "misses": list(misses)}


def _validate_savings_fixture(fixture: dict[str, Any]) -> None:
    if fixture.get("talk_track") != TALK_TRACK:
        raise ValueError("fixture talk_track must be 'we measure'")
    tasks = fixture.get("tasks")
    if not isinstance(tasks, list) or len(tasks) < 2:
        raise ValueError("fixture field 'tasks' must list at least two legit tasks")
    kinds: set[str] = set()
    ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("each task must be an object")
        task_id = task.get("id")
        kind = task.get("kind")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("each task must have a non-empty string 'id'")
        if task_id in ids:
            raise ValueError(f"duplicate task id {task_id!r}")
        ids.add(task_id)
        if kind not in _KINDS:
            raise ValueError(f"task {task_id!r} kind must be one of {sorted(_KINDS)}")
        kinds.add(str(kind))
        if not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            raise ValueError(f"task {task_id!r} must have a real prompt")
        ac = task.get("acceptance_criteria")
        if not isinstance(ac, list) or not ac or any(not isinstance(item, str) for item in ac):
            raise ValueError(f"task {task_id!r} must have acceptance_criteria strings")
        for arm_name in ("direct", "verdict"):
            arm = task.get(arm_name)
            if not isinstance(arm, dict):
                raise ValueError(f"task {task_id!r} missing {arm_name} arm")
            parse_measured_cost(arm)
            _quality(arm)
            _used_model(arm)
    if len(kinds) < 2:
        raise ValueError(
            "fixture must cover at least two of debug / refactor_tests / implement_from_ac"
        )


def _prov(value: bool | int) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=_FETCHED)
    )


def _metadata() -> MetadataSnapshot:
    records: list[ModelMetadataRecord] = []
    for row in _CATALOG:
        caps = CapabilityCaps(
            tools=_prov(row.tools),
            vision=_prov(row.vision),
            context=_prov(row.context) if row.context is not None else None,
        )
        records.append(
            ModelMetadataRecord(id=row.identity_id, caps=caps, omniroute_ids=(row.identity_id,))
        )
    return MetadataSnapshot(
        schema_version="1", refreshed_at=_FETCHED, sources={}, records=tuple(records)
    )


def _snapshot() -> OmniRouteAdmitSnapshot:
    catalog_rows = [{"id": row.identity_id, "owned_by": row.provider} for row in _CATALOG]
    free_tier = [
        {"modelId": row.free_model_id, "provider": row.provider, "freeType": "keyless"}
        for row in _CATALOG
        if row.free and row.free_model_id
    ]
    seen_providers: list[str] = []
    connections: list[dict[str, Any]] = []
    for row in _CATALOG:
        if row.provider in seen_providers:
            continue
        seen_providers.append(row.provider)
        connections.append({"provider": row.provider, "isActive": True, "testStatus": "active"})
    return snapshot_from_payloads(
        catalog={"data": catalog_rows},
        free_tier={"perModel": free_tier},
        providers={"connections": connections},
    )


def _passport(identity_id: str) -> ModelPassport:
    qualified = _NOW - timedelta(minutes=1)
    return ModelPassport(
        provider=identity_id.split("/", 1)[0],
        model_id=identity_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=_NOW + timedelta(minutes=10),
        latency_p95=40.0,
    )


def _ok_transport() -> Any:
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def _service_primary_model() -> str:
    """Constructor fallback only — never used as a measured completed-with identity."""
    for row in _CATALOG:
        if classify(row.identity_id) <= 1:
            return row.identity_id
    return _CATALOG[0].identity_id


def _service(workspace_root: Path) -> IntelligenceService:
    passports = {row.identity_id: _passport(row.identity_id) for row in _CATALOG}
    return IntelligenceService(
        primary_model=_service_primary_model(),
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=_snapshot(),
        execute_offload=False,
        passports=passports,
        confirm_transport=_ok_transport(),
        admit_now=_NOW,
        workspace_root=workspace_root,
        context_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
        metadata_snapshot=_metadata(),
    )


def _withhold_reasons(
    *,
    executed: bool,
    evidence_gaps: Sequence[str],
    pack_state: str | None,
    included_sources: list[Any],
    verdict_quality: Mapping[str, Any],
    direct_cost: MeasuredCost | None,
    verdict_cost: MeasuredCost | None,
) -> list[str]:
    """Every reason a claim is refused, in precedence order. Empty means claimable."""
    reasons: list[str] = []
    if not executed:
        reasons.append(WITHHOLD_SIMULATION)
    reasons.extend(evidence_gaps)
    if not verdict_quality["passed"]:
        reasons.append("quality_miss")
    if (direct_cost is not None and direct_cost.cache_hit) or (
        verdict_cost is not None and verdict_cost.cache_hit
    ):
        reasons.append("cache_hit_is_not_model_savings")
    if not savings_unlocked(pack_state) or not included_sources:
        reasons.append("pack_not_hydrated")
    if direct_cost is None or verdict_cost is None:
        reasons.append("cost_not_observed")
    elif verdict_cost.usd >= direct_cost.usd:
        reasons.append("no_cost_reduction")
    return list(dict.fromkeys(reasons))


def _arm_view(arm: Mapping[str, Any], *, measured_from: str) -> dict[str, Any]:
    """Cost/identity/quality for one arm as evidence rows, never as invented numbers."""
    return {"measured_from": measured_from, **_arm_cost_view(arm)}


def _arm_cost_view(arm: Mapping[str, Any]) -> dict[str, Any]:
    cost = parse_measured_cost(arm)
    return {
        "completed_with": _used_model(arm),
        "cost_usd": cost.usd,
        "tokens_in": cost.tokens_in,
        "tokens_out": cost.tokens_out,
        "cache_hit": cost.cache_hit,
        "cost_source": cost.source,
    }


def _cost_or_error(arm: Mapping[str, Any]) -> tuple[MeasuredCost | None, str | None]:
    try:
        return parse_measured_cost(arm), None
    except ValueError as exc:
        return None, str(exc)


def _execute(executor: ArmExecutor, request: ArmRequest) -> tuple[ArmExecution | None, str | None]:
    try:
        execution = executor(request)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if execution.arm != request.arm:
        return None, f"executor returned arm {execution.arm!r} for {request.arm!r}"
    return execution, None


def run_savings_bench(
    fixture_path: str | Path = DEFAULT_SAVINGS_FIXTURE_PATH,
    *,
    execute_arm: ArmExecutor | None = None,
    quality_evaluator: QualityEvaluator | None = None,
    gateway: str = "omniroute",
) -> dict[str, Any]:
    """Run the paired legit-task savings bench.

    Without ``execute_arm`` the run is a labeled simulation and can never
    claim savings. With it, both arms are executed on the same input hash and
    a claim requires complete, bound evidence.
    """
    path = resolve_fixture_path(fixture_path)
    fixture = cast(dict[str, Any], json.loads(path.read_text()))
    _validate_savings_fixture(fixture)
    workspace = resolve_fixture_workspace(
        path, str(fixture.get("workspace") or "benchmarks/fixtures/legit_workspace")
    )
    service = _service(workspace)
    evaluate = quality_evaluator or evaluate_output_against_checks
    executed_mode = execute_arm is not None
    tasks: list[dict[str, Any]] = []
    evidence_bundle: list[dict[str, Any]] = []
    for task in cast(list[dict[str, Any]], fixture["tasks"]):
        task_id = str(task["id"])
        prompt = str(task["prompt"])
        criteria = tuple(str(item) for item in task["acceptance_criteria"])
        input_hash = canonical_input_hash(prompt, criteria)
        decision = asyncio.run(service.route(prompt, criticality="low"))
        receipt = decision.admit_receipt if isinstance(decision.admit_receipt, dict) else {}
        pack_state = receipt.get("pack_state")
        included = list(receipt.get("included_sources") or receipt.get("included") or [])
        direct_fixture = cast(dict[str, Any], task[ARM_DIRECT])
        verdict_fixture = cast(dict[str, Any], task[ARM_VERDICT])
        direct_identity = _used_model(direct_fixture)
        verdict_routed = decision.model
        attempt_chain = tuple(dict.fromkeys([verdict_routed, *decision.alternatives]))

        direct_request = ArmRequest(
            arm=ARM_DIRECT,
            task_id=task_id,
            prompt=prompt,
            acceptance_criteria=criteria,
            input_hash=input_hash,
            model=direct_identity,
        )
        verdict_request = ArmRequest(
            arm=ARM_VERDICT,
            task_id=task_id,
            prompt=prompt,
            acceptance_criteria=criteria,
            input_hash=input_hash,
            model=verdict_routed,
            context_envelope=decision.context_pack_prompt,
            routed_model=verdict_routed,
            attempt_chain=attempt_chain,
            pack_digest=receipt.get("pack_digest"),
            prompt_digest=receipt.get("prompt_digest"),
        )

        direct_exec: ArmExecution | None = None
        verdict_exec: ArmExecution | None = None
        executor_errors: dict[str, str] = {}
        if execute_arm is not None:
            direct_exec, err = _execute(execute_arm, direct_request)
            if err:
                executor_errors[ARM_DIRECT] = err
            verdict_exec, err = _execute(execute_arm, verdict_request)
            if err:
                executor_errors[ARM_VERDICT] = err

        if executed_mode:
            direct_cost, direct_cost_err = (
                _cost_or_error(_arm_from_execution(direct_exec))
                if direct_exec is not None
                else (None, "not executed")
            )
            verdict_cost, verdict_cost_err = (
                _cost_or_error(_arm_from_execution(verdict_exec))
                if verdict_exec is not None
                else (None, "not executed")
            )
            gaps = execution_evidence_gaps(direct_request, direct_exec, cost_error=direct_cost_err)
            gaps += execution_evidence_gaps(
                verdict_request, verdict_exec, cost_error=verdict_cost_err
            )
            direct_quality = (
                evaluate(task, direct_exec).to_dict()
                if direct_exec is not None
                else QualityResult(False, ("not executed",), "none").to_dict()
            )
            verdict_quality = (
                evaluate(task, verdict_exec).to_dict()
                if verdict_exec is not None
                else QualityResult(False, ("not executed",), "none").to_dict()
            )
            direct_view = _execution_view(direct_exec, direct_cost)
            verdict_view = _execution_view(verdict_exec, verdict_cost)
            identity = (
                explain_identity(verdict_request, verdict_exec)
                if verdict_exec is not None
                else {
                    "routed": verdict_routed,
                    "completed_with": None,
                    "gateway": gateway,
                    "attempt_chain": list(attempt_chain),
                    "bound": False,
                    "explanation": "verdict arm was not executed",
                }
            )
            for execution in (direct_exec, verdict_exec):
                if execution is not None:
                    evidence_bundle.append({"task_id": task_id, **execution.to_evidence()})
        else:
            # Simulation: fixture numbers are echoed as *stated* values, never as
            # evidence. They are not bound to any execution and cannot claim.
            direct_cost = parse_measured_cost(direct_fixture)
            verdict_cost = parse_measured_cost(verdict_fixture)
            gaps = []
            direct_quality = _quality(direct_fixture)
            verdict_quality = _quality(verdict_fixture)
            direct_view = _arm_view(direct_fixture, measured_from="fixture (not executed)")
            verdict_view = _arm_view(verdict_fixture, measured_from="fixture (not executed)")
            stated = verdict_view["completed_with"]
            identity = {
                "routed": verdict_routed,
                "completed_with": stated,
                "gateway": None,
                "attempt_chain": list(attempt_chain),
                "bound": False,
                "explanation": (
                    "fixture-stated identity is not bound to any execution receipt"
                    + (
                        ""
                        if stated == verdict_routed
                        else f"; live chooser routed {verdict_routed}"
                    )
                ),
            }

        reasons = _withhold_reasons(
            executed=executed_mode and direct_exec is not None and verdict_exec is not None,
            evidence_gaps=gaps,
            pack_state=str(pack_state) if pack_state is not None else None,
            included_sources=included,
            verdict_quality=verdict_quality,
            direct_cost=direct_cost,
            verdict_cost=verdict_cost,
        )
        cost_delta = (
            round(verdict_cost.usd - direct_cost.usd, 6)
            if direct_cost is not None and verdict_cost is not None
            else None
        )
        tasks.append(
            {
                "task_id": task_id,
                "kind": str(task["kind"]),
                "acceptance_criteria": list(criteria),
                "input_hash": input_hash,
                "executed": executed_mode and direct_exec is not None and verdict_exec is not None,
                "executor_errors": executor_errors,
                "direct": {
                    **direct_view,
                    "model": direct_view["completed_with"],
                    "quality": direct_quality,
                },
                "verdict": {
                    **verdict_view,
                    "routed": verdict_routed,
                    "model": verdict_routed,
                    "task_class": decision.task_class,
                    "pack_state": pack_state,
                    "pack_digest": receipt.get("pack_digest"),
                    "prompt_digest": receipt.get("prompt_digest"),
                    "included_sources": included,
                    "selected_because": receipt.get("selected_because"),
                    "quality": verdict_quality,
                },
                "identity_binding": identity,
                "deltas": {"cost_usd": cost_delta},
                "savings_claimed": not reasons,
                "withhold_reason": reasons[0] if reasons else None,
                "withhold_reasons": reasons,
            }
        )
    claimed = [item for item in tasks if item["savings_claimed"]]

    def _count(reason: str) -> int:
        return sum(1 for item in tasks if reason in item["withhold_reasons"])

    measured = [
        item for item in tasks if item["executed"] and item["deltas"]["cost_usd"] is not None
    ]
    stated = [
        item for item in tasks if not item["executed"] and item["deltas"]["cost_usd"] is not None
    ]
    return {
        "schema_version": SAVINGS_REPORT_SCHEMA_VERSION,
        "mode": MODE_LIVE_PAIRED if executed_mode else MODE_SIMULATION,
        "executed": executed_mode,
        "claims_allowed": executed_mode,
        "talk_track": TALK_TRACK,
        "fixture_path": str(path),
        "fixture_digest_sha256": _fixture_digest(fixture),
        "workspace": str(workspace),
        "gateway": gateway if executed_mode else None,
        "tasks": tasks,
        "evidence_bundle": evidence_bundle,
        "aggregate": {
            "task_count": len(tasks),
            "executed_count": sum(1 for item in tasks if item["executed"]),
            "savings_claimed_count": len(claimed),
            "simulation_count": _count(WITHHOLD_SIMULATION),
            "quality_miss_count": _count("quality_miss"),
            "cache_hit_count": _count("cache_hit_is_not_model_savings"),
            "measured_cost_delta_usd": round(
                sum(float(item["deltas"]["cost_usd"]) for item in measured), 6
            ),
            # Fixture-stated deltas from unexecuted arms. Not measurements.
            "stated_cost_delta_usd": round(
                sum(float(item["deltas"]["cost_usd"]) for item in stated), 6
            ),
            "claimed_cost_delta_usd": round(
                sum(float(item["deltas"]["cost_usd"]) for item in claimed), 6
            ),
        },
        "provenance": {
            "mode": (
                "live-paired: both arms executed by execute_arm on the same input_hash"
                if executed_mode
                else "simulation: fixture values are stated, not observed; no arm was executed; "
                "savings cannot be claimed"
            ),
            "cost": (
                "X-OmniRoute-Response-Cost / Tokens-In/Out headers or matching receipt fields, "
                "bound to execution_id"
            ),
            "cache_hit": (
                "cache hit on cheaper or frontier models is labeled and never sold as model savings"
            ),
            "quality": (
                "evaluated from produced output against task checks; passed=true with misses "
                "is rejected as contradictory"
            ),
            "completed_with": (
                "each arm's identity comes from the execution receipt (X-OmniRoute-Model / "
                "completed_with); the Verdict arm binds routed → completed via the attempt chain"
            ),
            "evidence_bundle": (
                "privacy-reviewed: execution IDs, input/output digests, identities and "
                "x-omniroute-* headers only; no prompt or output text"
            ),
        },
    }


def _arm_from_execution(execution: ArmExecution) -> dict[str, Any]:
    return {
        "headers": dict(execution.headers),
        "receipt": dict(execution.receipt),
        "completed_with": execution.completed_with,
    }


def _execution_view(execution: ArmExecution | None, cost: MeasuredCost | None) -> dict[str, Any]:
    if execution is None:
        return {
            "measured_from": "not executed",
            "execution_id": None,
            "completed_with": None,
            "cost_usd": None,
            "tokens_in": None,
            "tokens_out": None,
            "cache_hit": None,
            "cost_source": None,
        }
    return {
        "measured_from": f"execution {execution.execution_id}",
        "execution_id": execution.execution_id,
        "completed_with": execution.completed_with,
        "cost_usd": None if cost is None else cost.usd,
        "tokens_in": None if cost is None else cost.tokens_in,
        "tokens_out": None if cost is None else cost.tokens_out,
        "cache_hit": None if cost is None else cost.cache_hit,
        "cost_source": None if cost is None else cost.source,
    }


def format_savings_report(report: dict[str, Any]) -> str:
    """Render a talk-track-safe savings report."""
    lines = [
        f"mode: {report['mode']}",
        f"claims_allowed: {str(report.get('claims_allowed', False)).lower()}",
        f"talk_track: {report['talk_track']}",
        f"tasks: {report['aggregate']['task_count']}",
        f"executed: {report['aggregate'].get('executed_count', 0)}",
        f"savings_claimed: {report['aggregate']['savings_claimed_count']}",
        f"quality_misses: {report['aggregate']['quality_miss_count']}",
        f"cache_hits: {report['aggregate'].get('cache_hit_count', 0)}",
        f"measured_cost_delta_usd: {report['aggregate']['measured_cost_delta_usd']}",
        f"stated_cost_delta_usd: {report['aggregate'].get('stated_cost_delta_usd', 0)}",
        f"claimed_cost_delta_usd: {report['aggregate']['claimed_cost_delta_usd']}",
    ]
    for task in cast(list[dict[str, Any]], report["tasks"]):
        if task["savings_claimed"]:
            claim = "claimed"
        else:
            claim = "withheld:" + ",".join(
                task.get("withhold_reasons") or [task["withhold_reason"]]
            )
        direct_used = task["direct"].get("completed_with") or task["direct"].get("model")
        verdict_used = task["verdict"].get("completed_with")
        routed = task["verdict"].get("routed") or task["verdict"].get("model")
        cache_hit = bool(task["verdict"].get("cache_hit"))
        lines.append(
            f"- {task['task_id']} ({task['kind']}): {claim} "
            f"delta={task['deltas']['cost_usd']} pack={task['verdict']['pack_state']} "
            f"direct_used={direct_used} verdict_used={verdict_used} "
            f"routed={routed} cache_hit={str(cache_hit).lower()}"
        )
    if not report.get("claims_allowed", False):
        lines.append(
            "NOTE: simulation — no arm was executed; fixture values are stated, not observed; "
            "savings cannot be claimed from this report"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_SAVINGS_FIXTURE_PATH",
    "MODE_LIVE_PAIRED",
    "MODE_SIMULATION",
    "TALK_TRACK",
    "WITHHOLD_SIMULATION",
    "MeasuredCost",
    "format_savings_report",
    "parse_measured_cost",
    "run_savings_bench",
]
