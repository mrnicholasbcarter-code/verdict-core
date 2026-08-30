"""Live routing golden path: classify, select cheaper-first, explain, execute."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from verdict.omniroute_catalog import DEFAULT_CATALOG_FRESHNESS_SECONDS

SCHEMA_VERSION = "golden-path/v1"
NAMED_CHECK_OBJECT = {"golden_path": "ok"}
COST_RANK = {"local": 0, "free": 1, "cheaper": 2, "paid": 3}
OPAQUE_PREFIXES = ("auto/", "auto:", "openrouter/auto")
CostClass = Literal["local", "free", "cheaper", "paid"]
DropReason = Literal[
    "policy",
    "health",
    "capability",
    "unclassified",
    "stale",
    "opaque_mix",
    "cost",
    "quota",
]


class LiveRoutingError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LiveSurfaceBlockedError(LiveRoutingError):
    def __init__(self, message: str = "live gateway or provider is unreachable") -> None:
        super().__init__("live_surface_blocked", message)


LiveSurfaceBlocked = LiveSurfaceBlockedError


@dataclass(frozen=True)
class ConcreteIdentity:
    identity_id: str
    provider_id: str
    gateway_id: str
    cost_class: CostClass | None
    context_limit: int | None
    output_limit: int | None
    tools: bool | None
    modalities: tuple[str, ...] | None
    spec_captured_at: datetime
    freshness_seconds: int = DEFAULT_CATALOG_FRESHNESS_SECONDS

    def specs_complete(self) -> bool:
        return None not in (
            self.cost_class,
            self.context_limit,
            self.output_limit,
            self.tools,
            self.modalities,
        )

    def is_fresh(self, moment: datetime) -> bool:
        return self.spec_captured_at + timedelta(seconds=self.freshness_seconds) >= moment


@dataclass(frozen=True)
class Mix:
    mix_id: str
    steps: tuple[str, ...]
    opaque: bool = False

    @property
    def named(self) -> bool:
        return bool(self.steps) and all(step and not _opaque_id(step) for step in self.steps)


@dataclass(frozen=True)
class Candidate:
    ref: str
    status: Literal["kept", "dropped"]
    reason: DropReason | None
    identity: ConcreteIdentity | None = None
    mix: Mix | None = None


@dataclass(frozen=True)
class RouteSelection:
    chosen: Candidate
    paid_used: bool
    cheaper_available: bool
    ordered: tuple[Candidate, ...]

    def __post_init__(self) -> None:
        if self.paid_used and self.cheaper_available:
            raise LiveRoutingError("cost", "paid selected while cheaper kept candidate exists")


@dataclass(frozen=True)
class UsageSnapshot:
    provider_id: str
    source: str
    used_percent: float | None
    remaining_percent: float | None
    resets_at: str | None
    exhausted: bool


@dataclass
class GoldenPathReceipt:
    unit_id: str
    endpoint: str
    chosen_id: str
    paid_used: bool
    cheaper_available: bool
    attempts: list[dict[str, Any]]
    checker_passed: bool
    catalog_captured_at: str
    degraded: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "unit_id": self.unit_id,
            "endpoint": self.endpoint,
            "chosen_id": self.chosen_id,
            "paid_used": self.paid_used,
            "cheaper_available": self.cheaper_available,
            "attempts": list(self.attempts),
            "checker_passed": self.checker_passed,
            "catalog_captured_at": self.catalog_captured_at,
            "error": self.error,
        }
        if self.degraded:
            raise LiveRoutingError("live_surface_blocked", "fixture catalog cannot emit a pass receipt")
        return payload


def _opaque_id(identity_id: str) -> bool:
    lowered = identity_id.lower()
    return lowered.startswith(OPAQUE_PREFIXES) or lowered in {"auto", "openrouter/auto"}


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cost_class_from_row(row: MappingLike) -> CostClass | None:
    nested = row.get("verdict")
    explicit = row.get("cost_class")
    if not explicit and isinstance(nested, dict):
        explicit = nested.get("cost_class")
    if explicit in COST_RANK:
        return explicit  # type: ignore[return-value]
    owned = row.get("owned_by")
    if isinstance(owned, str) and owned.lower() in {"ollama", "lmstudio", "vllm", "llamacpp", "local"}:
        return "local"
    pricing = row.get("pricing")
    if isinstance(pricing, dict):
        prompt = _as_float(pricing.get("prompt") or pricing.get("input"))
        completion = _as_float(pricing.get("completion") or pricing.get("output"))
        if prompt is not None:
            completion = 0.0 if completion is None else completion
            if prompt == 0.0 and completion == 0.0:
                return "free"
            if prompt <= 1.0:
                return "cheaper"
            return "paid"
    return None


MappingLike = Mapping[str, Any]


def identity_from_row(
    row: MappingLike,
    *,
    gateway_id: str,
    captured_at: datetime,
    freshness_seconds: int = DEFAULT_CATALOG_FRESHNESS_SECONDS,
) -> ConcreteIdentity:
    identity_id = str(row.get("id") or "")
    caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
    modalities = row.get("modalities")
    if isinstance(modalities, list):
        mods: tuple[str, ...] | None = tuple(str(item) for item in modalities)
    elif caps.get("vision") is True:
        mods = ("text", "vision")
    elif caps.get("vision") is False or "vision" not in caps:
        mods = ("text",)
    else:
        mods = None
    context = caps.get("context") or row.get("context_length") or row.get("context_window")
    output = caps.get("max_output") or row.get("max_output_tokens")
    tools = caps.get("tools")
    if tools is None and isinstance(caps.get("tool_calling"), bool):
        tools = caps.get("tool_calling")
    provider = row.get("owned_by") or (identity_id.split("/")[0] if "/" in identity_id else "unknown")
    return ConcreteIdentity(
        identity_id=identity_id,
        provider_id=str(provider),
        gateway_id=gateway_id,
        cost_class=cost_class_from_row(row),
        context_limit=int(context) if isinstance(context, (int, float)) else None,
        output_limit=int(output) if isinstance(output, (int, float)) else None,
        tools=tools if isinstance(tools, bool) else None,
        modalities=mods,
        spec_captured_at=captured_at,
        freshness_seconds=freshness_seconds,
    )


def classify_identities(
    identities: Sequence[ConcreteIdentity],
    *,
    denylist: frozenset[str] = frozenset(),
    mixes: Sequence[Mix] = (),
    usage: Sequence[UsageSnapshot] = (),
    now: datetime | None = None,
) -> list[Candidate]:
    moment = now or datetime.now(timezone.utc)
    exhausted = {item.provider_id for item in usage if item.exhausted}
    out: list[Candidate] = []
    for identity in identities:
        reason: DropReason | None = None
        if _opaque_id(identity.identity_id):
            reason = "opaque_mix"
        elif identity.identity_id in denylist:
            reason = "policy"
        elif not identity.is_fresh(moment):
            reason = "stale"
        elif not identity.specs_complete():
            reason = "unclassified"
        elif identity.provider_id in exhausted:
            reason = "quota"
        status = "dropped" if reason else "kept"
        out.append(Candidate(identity.identity_id, status, reason, identity=identity))
    by_id = {item.identity_id: item for item in identities}
    for mix in mixes:
        if mix.opaque or not mix.named:
            out.append(Candidate(mix.mix_id, "dropped", "opaque_mix", mix=mix))
            continue
        remaining: ConcreteIdentity | None = None
        incomplete = False
        for step_id in mix.steps:
            step = by_id.get(step_id)
            if step is None or not step.specs_complete() or not step.is_fresh(moment):
                incomplete = True
                break
            if step.identity_id in denylist or step.provider_id in exhausted:
                continue
            if remaining is None:
                remaining = step
        if incomplete:
            out.append(Candidate(mix.mix_id, "dropped", "unclassified", mix=mix))
            continue
        if remaining is None:
            out.append(Candidate(mix.mix_id, "dropped", "quota", mix=mix))
            continue
        out.append(Candidate(mix.mix_id, "kept", None, identity=remaining, mix=mix))
    return out


def _sort_key(candidate: Candidate) -> tuple[int, str]:
    identity = candidate.identity
    if identity is None or identity.cost_class is None:
        return (99, candidate.ref)
    return (COST_RANK[identity.cost_class], identity.identity_id)


def select_route(candidates: Sequence[Candidate]) -> RouteSelection:
    kept = [item for item in candidates if item.status == "kept" and item.identity is not None]
    if not kept:
        raise LiveRoutingError("no_qualified_candidate", "no qualified candidate remains")
    ordered = tuple(sorted(kept, key=_sort_key))
    chosen = ordered[0]
    cheaper_available = any(
        item.identity is not None and item.identity.cost_class in {"local", "free", "cheaper"}
        for item in ordered
    )
    paid_used = chosen.identity is not None and chosen.identity.cost_class == "paid"
    return RouteSelection(chosen, paid_used, cheaper_available, ordered)


def failover_order(selection: RouteSelection) -> list[Candidate]:
    return list(selection.ordered)


def explain(candidates: Sequence[Candidate], selection: RouteSelection | None) -> dict[str, Any]:
    chosen_id = selection.chosen.ref if selection else None
    return {
        "kept": [item.ref for item in candidates if item.status == "kept"],
        "dropped": [
            {"id": item.ref, "reason": item.reason} for item in candidates if item.status == "dropped"
        ],
        "chosen": chosen_id,
        "paid_used": None if selection is None else selection.paid_used,
        "cheaper_available": None if selection is None else selection.cheaper_available,
    }


_SECRET = re.compile(r"(?i)(api[_-]?key|password|secret|token|authorization|credential)")


def strip_secrets(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if _SECRET.search(key) or (isinstance(value, str) and _SECRET.search(value)):
            continue
        if key in {"prompt", "completion", "messages", "tool_arguments"}:
            continue
        clean[key] = value
    return clean


def named_check_passes(body: str) -> bool:
    try:
        parsed = json.loads(body.strip())
    except json.JSONDecodeError:
        return False
    return parsed == NAMED_CHECK_OBJECT


