"""Paired frontier-direct vs Verdict savings bench (BOD-101).

Cost is parsed only from OmniRoute response headers or the same fields on a
receipt. Cache hits are labeled and never sold as model savings. Quality misses
are reported and withhold a savings claim. The Verdict arm must actually route
so ``pack_state=hydrated`` with real ``included_sources`` is observed, not
asserted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
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

DEFAULT_SAVINGS_FIXTURE_PATH = default_fixture_path("benchmarks/fixtures/legit_paired_savings.json")
TALK_TRACK = "we measure"
SAVINGS_REPORT_SCHEMA_VERSION = "1"
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
    passed = raw.get("passed")
    if not isinstance(passed, bool):
        raise ValueError("quality.passed must be a boolean")
    misses = raw.get("misses", [])
    if not isinstance(misses, list) or any(not isinstance(item, str) for item in misses):
        raise ValueError("quality.misses must be a list of strings")
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


def _withhold_reason(
    *,
    pack_state: str | None,
    included_sources: list[Any],
    verdict_quality: Mapping[str, Any],
    direct_cost: MeasuredCost,
    verdict_cost: MeasuredCost,
) -> str | None:
    if not verdict_quality["passed"]:
        return "quality_miss"
    if direct_cost.cache_hit or verdict_cost.cache_hit:
        return "cache_hit_is_not_model_savings"
    if not savings_unlocked(pack_state) or not included_sources:
        return "pack_not_hydrated"
    if verdict_cost.usd >= direct_cost.usd:
        return "no_cost_reduction"
    return None


def run_savings_bench(fixture_path: str | Path = DEFAULT_SAVINGS_FIXTURE_PATH) -> dict[str, Any]:
    """Run the offline paired legit-task savings bench."""
    path = resolve_fixture_path(fixture_path)
    fixture = cast(dict[str, Any], json.loads(path.read_text()))
    _validate_savings_fixture(fixture)
    workspace = resolve_fixture_workspace(
        path, str(fixture.get("workspace") or "benchmarks/fixtures/legit_workspace")
    )
    service = _service(workspace)
    tasks: list[dict[str, Any]] = []
    for task in cast(list[dict[str, Any]], fixture["tasks"]):
        decision = asyncio.run(service.route(str(task["prompt"]), criticality="low"))
        receipt = decision.admit_receipt or {}
        pack_state = receipt.get("pack_state") if isinstance(receipt, dict) else None
        included = (
            list(receipt.get("included_sources") or receipt.get("included") or [])
            if isinstance(receipt, dict)
            else []
        )
        direct_arm = cast(dict[str, Any], task["direct"])
        verdict_arm = cast(dict[str, Any], task["verdict"])
        direct_cost = parse_measured_cost(direct_arm)
        verdict_cost = parse_measured_cost(verdict_arm)
        direct_quality = _quality(direct_arm)
        verdict_quality = _quality(verdict_arm)
        withhold = _withhold_reason(
            pack_state=str(pack_state) if pack_state is not None else None,
            included_sources=included,
            verdict_quality=verdict_quality,
            direct_cost=direct_cost,
            verdict_cost=verdict_cost,
        )
        cost_delta = round(verdict_cost.usd - direct_cost.usd, 6)
        direct_used = _used_model(direct_arm)
        verdict_used = _used_model(verdict_arm)
        verdict_routed = decision.model
        tasks.append(
            {
                "task_id": str(task["id"]),
                "kind": str(task["kind"]),
                "acceptance_criteria": list(task["acceptance_criteria"]),
                "direct": {
                    "completed_with": direct_used,
                    "model": direct_used,
                    "cost_usd": direct_cost.usd,
                    "tokens_in": direct_cost.tokens_in,
                    "tokens_out": direct_cost.tokens_out,
                    "cache_hit": direct_cost.cache_hit,
                    "cost_source": direct_cost.source,
                    "quality": direct_quality,
                },
                "verdict": {
                    "completed_with": verdict_used,
                    "routed": verdict_routed,
                    "model": verdict_routed,
                    "task_class": decision.task_class,
                    "pack_state": pack_state,
                    "included_sources": included,
                    "selected_because": receipt.get("selected_because")
                    if isinstance(receipt, dict)
                    else None,
                    "cost_usd": verdict_cost.usd,
                    "tokens_in": verdict_cost.tokens_in,
                    "tokens_out": verdict_cost.tokens_out,
                    "cache_hit": verdict_cost.cache_hit,
                    "cost_source": verdict_cost.source,
                    "quality": verdict_quality,
                },
                "deltas": {"cost_usd": cost_delta},
                "savings_claimed": withhold is None,
                "withhold_reason": withhold,
            }
        )
    claimed = [item for item in tasks if item["savings_claimed"]]
    return {
        "schema_version": SAVINGS_REPORT_SCHEMA_VERSION,
        "mode": "local-savings",
        "talk_track": TALK_TRACK,
        "fixture_path": str(path),
        "fixture_digest_sha256": _fixture_digest(fixture),
        "workspace": str(workspace),
        "tasks": tasks,
        "aggregate": {
            "task_count": len(tasks),
            "savings_claimed_count": len(claimed),
            "quality_miss_count": sum(
                1 for item in tasks if item["withhold_reason"] == "quality_miss"
            ),
            "cache_hit_count": sum(
                1 for item in tasks if item["withhold_reason"] == "cache_hit_is_not_model_savings"
            ),
            "measured_cost_delta_usd": round(
                sum(float(item["deltas"]["cost_usd"]) for item in tasks), 6
            ),
            "claimed_cost_delta_usd": round(
                sum(float(item["deltas"]["cost_usd"]) for item in claimed), 6
            ),
        },
        "provenance": {
            "cost": "X-OmniRoute-Response-Cost / Tokens-In/Out headers or matching receipt fields",
            "cache_hit": (
                "cache hit on cheaper or frontier models is labeled and never sold as model savings"
            ),
            "quality": "quality miss is reported and never sold as savings",
            "completed_with": (
                "each arm must specify the model the measured cost belongs to "
                "(X-OmniRoute-Model / completed_with); Verdict also stamps "
                "routed=decision.model from the live chooser"
            ),
        },
    }


def format_savings_report(report: dict[str, Any]) -> str:
    """Render a talk-track-safe savings report."""
    lines = [
        f"mode: {report['mode']}",
        f"talk_track: {report['talk_track']}",
        f"tasks: {report['aggregate']['task_count']}",
        f"savings_claimed: {report['aggregate']['savings_claimed_count']}",
        f"quality_misses: {report['aggregate']['quality_miss_count']}",
        f"cache_hits: {report['aggregate'].get('cache_hit_count', 0)}",
        f"measured_cost_delta_usd: {report['aggregate']['measured_cost_delta_usd']}",
        f"claimed_cost_delta_usd: {report['aggregate']['claimed_cost_delta_usd']}",
    ]
    for task in cast(list[dict[str, Any]], report["tasks"]):
        claim = "claimed" if task["savings_claimed"] else f"withheld:{task['withhold_reason']}"
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
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_SAVINGS_FIXTURE_PATH",
    "TALK_TRACK",
    "MeasuredCost",
    "format_savings_report",
    "parse_measured_cost",
    "run_savings_bench",
]
