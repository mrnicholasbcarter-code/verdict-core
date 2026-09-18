"""Admit free-tier ∩ active-provider concrete identities for offloadable work.

Live OmniRoute surfaces used:

* ``GET /v1/models`` — concrete executable catalog identities
* ``GET /api/free-tier/summary`` — positively-free metadata (``perModel``)
* ``GET /api/providers`` — connections with ``isActive``

Metadata-only free-tier rows that cannot be resolved to a catalog identity, or
whose provider is inactive/unconnected, become *named drops* rather than fake
green. Opaque ``auto/*`` aliases are never admitted. An empty intersection
fails closed — the caller must not treat frontier-primary fallback as success.

Serve cheap-path callers then intersect this receipt with fresh prove-at-rest
passports and a budgeted confirm probe (see ``verdict.admit_prove_confirm``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from verdict.availability import is_opaque_route_id
from verdict.classifier import classify
from verdict.context_pack import ContextPackCompiler, ContextPackSlot, ContextPlan
from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict
from verdict.free_route_harvest import free_status
from verdict.models import ModelInfo

REASON_OPAQUE_AUTO = "opaque_auto"
REASON_NOT_FREE_TIER = "not_free_tier"
REASON_INACTIVE_UNCONNECTED = "inactive_unconnected"
REASON_METADATA_GHOST = "metadata_ghost"
_COMBO_PREFIXES = frozenset({"claude", "combo"})
_ALIAS_PREFIXES = frozenset({"oc", "kr", "cf", "or", "nv"})
_SMALL_TOKENS = ("nano", "flash", "haiku", "mini", "small", "lite", "instant")

NO_ELIGIBLE_TARGET = "no_eligible_target"
FAIL_CLOSED_REASON = "fail_closed — empty free∩active ∩ fresh-passport ∩ confirmed intersection"


class LiveAdmitError(RuntimeError):
    """Raised when a required OmniRoute admit surface cannot be read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProviderConnection:
    provider: str
    is_active: bool
    test_status: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class FreeTierModel:
    model_id: str
    provider: str
    free_type: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class CatalogIdentity:
    identity_id: str
    provider: str


@dataclass(frozen=True)
class NamedDrop:
    model_id: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict[str, str]:
        payload = {"model": self.model_id, "reason": self.reason}
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class OmniRouteAdmitSnapshot:
    """Normalized live (or fixture) surfaces for free∩active admit."""

    catalog: tuple[CatalogIdentity, ...]
    free_tier: tuple[FreeTierModel, ...]
    connections: tuple[ProviderConnection, ...]

    @property
    def active_providers(self) -> frozenset[str]:
        return frozenset(row.provider for row in self.connections if row.is_active)

    @property
    def free_tier_providers(self) -> frozenset[str]:
        return frozenset(row.provider for row in self.free_tier if row.provider)


_CHEAP_PATH_EPOCH = "1970-01-01T00:00:00Z"


@dataclass(frozen=True)
class NamedOmission:
    """Something left out of a cheap-path context pack, with why."""

    name: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason}


@dataclass(frozen=True)
class CheapPathContextPack:
    """Compiled task pack used on the free∩active offload path."""

    pack_digest: str
    compiled_prompt: str
    omissions: tuple[NamedOmission, ...]
    pack_id: str
    plan_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pack_digest": self.pack_digest,
            "pack_id": self.pack_id,
            "plan_digest": self.plan_digest,
            "omissions": [item.to_dict() for item in self.omissions],
        }


def build_cheap_path_context_pack(
    task: str,
    *,
    candidate_id: str,
    token_budget: int = 4096,
    extra_slots: Sequence[ContextPackSlot] | None = None,
) -> CheapPathContextPack:
    """Compile a deterministic context pack for cheap-path offload.

    The task itself is always included as an ``instructions`` unit. Optional
    ``extra_slots`` may be supplied (tests/fixtures); compiler exclusions
    become named omissions on the receipt. Timestamps are pinned so the pack
    digest is stable for identical inputs.
    """
    if not isinstance(task, str) or not task.strip():
        raise ValueError("task must be a non-empty string")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("candidate_id must be a non-empty string")
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or token_budget < 1:
        raise ValueError("token_budget must be a positive integer")

    task_slot = ContextPackSlot(
        slot_type="instructions",
        key="task",
        content=task,
        source="cheap_path",
        created_at=0.0,
        source_uri="urn:verdict:task",
    )
    slots = (task_slot, *(extra_slots or ()))
    units = []
    for slot in slots:
        unit = slot.to_unit()
        units.append(
            replace(
                unit, observed_at=_CHEAP_PATH_EPOCH, retrieved_at=_CHEAP_PATH_EPOCH, created_at=0.0
            )
        )
    plan = ContextPlan(
        plan_id=f"cheap:{candidate_id}",
        candidate_id=candidate_id,
        token_budget=token_budget,
        created_at=_CHEAP_PATH_EPOCH,
    )
    pack = ContextPackCompiler().compile_units(tuple(units), plan)
    omissions = tuple(
        NamedOmission(name=decision.unit_id, reason=decision.reason)
        for decision in pack.decisions
        if decision.action == "exclude"
    )
    return CheapPathContextPack(
        pack_digest=pack.digest,
        compiled_prompt=pack.compiled_prompt,
        omissions=omissions,
        pack_id=pack.pack_id,
        plan_digest=pack.plan_digest or plan.digest,
    )


@dataclass(frozen=True)
class FreeTierAdmitReceipt:
    admitted: tuple[str, ...]
    exclusions: tuple[NamedDrop, ...]
    chosen: str | None
    empty_intersection: bool
    active_providers: tuple[str, ...]
    free_tier_providers: tuple[str, ...]
    pack_digest: str | None = None
    omissions: tuple[NamedOmission, ...] = ()
    passport: tuple[Any, ...] = ()
    confirm: tuple[Any, ...] = ()
    selected_because: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": list(self.admitted),
            "exclusions": [item.to_dict() for item in self.exclusions],
            "chosen": self.chosen,
            "empty_intersection": self.empty_intersection,
            "active_providers": list(self.active_providers),
            "free_tier_providers": list(self.free_tier_providers),
            "pack_digest": self.pack_digest,
            "omissions": [item.to_dict() for item in self.omissions],
            "passport": [
                item.to_dict() if hasattr(item, "to_dict") else item for item in self.passport
            ],
            "confirm": [
                item.to_dict() if hasattr(item, "to_dict") else item for item in self.confirm
            ],
            "selected_because": self.selected_because,
        }

    def as_eligibility_result(self, snapshot: OmniRouteAdmitSnapshot) -> EligibilityResult:
        by_id = {item.identity_id: item for item in snapshot.catalog}
        admitted_models: list[ModelInfo] = []
        for model_id in self.admitted:
            identity = by_id.get(model_id)
            provider = identity.provider if identity is not None else _provider_of(model_id)
            admitted_models.append(
                ModelInfo(
                    id=model_id,
                    provider=provider,
                    capability_tier=classify(model_id),
                    is_available=True,
                    availability_state="eligible",
                    source="free_tier_active_admit",
                )
            )
        records: list[EligibilityRecord] = [
            EligibilityRecord(
                model_id=model.id,
                provider=model.provider,
                admitted=True,
                verdict=EligibilityVerdict.ELIGIBLE,
                state="eligible",
                source="free_tier_active_admit",
                reason="free-tier ∩ active provider",
            )
            for model in admitted_models
        ]
        for drop in self.exclusions:
            records.append(
                EligibilityRecord(
                    model_id=drop.model_id,
                    provider=_provider_of(drop.model_id),
                    admitted=False,
                    verdict=_verdict_for_reason(drop.reason),
                    state="excluded",
                    source="free_tier_active_admit",
                    reason=drop.detail or drop.reason,
                )
            )
        return EligibilityResult(admitted=admitted_models, records=records)


def _verdict_for_reason(reason: str) -> EligibilityVerdict:
    mapping = {
        REASON_OPAQUE_AUTO: EligibilityVerdict.OPAQUE_AUTO,
        REASON_NOT_FREE_TIER: EligibilityVerdict.NOT_FREE_TIER,
        REASON_INACTIVE_UNCONNECTED: EligibilityVerdict.INACTIVE_UNCONNECTED,
        REASON_METADATA_GHOST: EligibilityVerdict.METADATA_GHOST,
        "no_passport": EligibilityVerdict.NO_PASSPORT,
        "passport_stale": EligibilityVerdict.PASSPORT_STALE,
        "confirm_failed": EligibilityVerdict.CONFIRM_FAILED,
        "confirm_budget_exhausted": EligibilityVerdict.CONFIRM_FAILED,
        "confirm_unavailable": EligibilityVerdict.CONFIRM_FAILED,
    }
    return mapping.get(reason, EligibilityVerdict.NOT_LIVE_ELIGIBLE)


def _provider_of(identity_id: str) -> str:
    if "/" in identity_id:
        return identity_id.split("/", 1)[0]
    return identity_id or "unknown"


def parse_provider_connections(payload: object) -> tuple[ProviderConnection, ...]:
    """Parse ``GET /api/providers`` (``connections[].isActive``)."""
    rows: list[Any] = []
    if isinstance(payload, Mapping):
        raw = payload.get("connections", payload.get("providers", payload.get("data")))
        if isinstance(raw, list):
            rows = raw
        elif isinstance(payload.get("provider"), str) or "isActive" in payload:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    out: list[ProviderConnection] = []
    seen: set[tuple[str, bool, str | None]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        provider = row.get("provider") or row.get("id")
        if not isinstance(provider, str) or not provider.strip():
            continue
        name = row.get("name")
        test_status = row.get("testStatus") or row.get("test_status")
        conn = ProviderConnection(
            provider=provider.strip(),
            is_active=row.get("isActive") is True,
            test_status=str(test_status) if isinstance(test_status, str) else None,
            name=str(name) if isinstance(name, str) else None,
        )
        key = (conn.provider, conn.is_active, conn.test_status)
        if key in seen:
            continue
        seen.add(key)
        out.append(conn)
    return tuple(out)


def parse_free_tier_models(payload: object) -> tuple[FreeTierModel, ...]:
    """Parse ``GET /api/free-tier/summary`` ``perModel`` rows."""
    raw: object
    if isinstance(payload, Mapping):
        raw = payload.get("perModel")
        if raw is None:
            raw = payload.get("models", payload.get("data", payload.get("items")))
    else:
        raw = payload
    if not isinstance(raw, list):
        return ()
    out: list[FreeTierModel] = []
    for row in raw:
        if isinstance(row, str) and row.strip():
            out.append(FreeTierModel(model_id=row.strip(), provider=_provider_of(row.strip())))
            continue
        if not isinstance(row, Mapping):
            continue
        model_id = row.get("modelId") or row.get("model_id") or row.get("id") or row.get("model")
        provider = row.get("provider") or row.get("owned_by")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        model_id = model_id.strip()
        if not isinstance(provider, str) or not provider.strip():
            provider = _provider_of(model_id)
        free_type = row.get("freeType") or row.get("free_type")
        display = row.get("displayName") or row.get("name")
        out.append(
            FreeTierModel(
                model_id=model_id,
                provider=provider.strip(),
                free_type=str(free_type) if isinstance(free_type, str) else None,
                display_name=str(display) if isinstance(display, str) else None,
            )
        )
    return tuple(out)


def parse_catalog_identities(payload: object) -> tuple[CatalogIdentity, ...]:
    """Parse OpenAI-compatible ``/v1/models`` or a management catalog envelope."""
    rows: list[Any]
    if isinstance(payload, Mapping) and isinstance(payload.get("catalog"), Mapping):
        rows = []
        catalog = payload["catalog"]
        assert isinstance(catalog, Mapping)
        for provider, group in catalog.items():
            if not isinstance(group, Mapping) or not isinstance(group.get("models"), list):
                continue
            for raw_row in group["models"]:
                if isinstance(raw_row, Mapping):
                    item = dict(raw_row)
                    item.setdefault("provider", str(provider))
                    item.setdefault("owned_by", str(provider))
                    rows.append(item)
        payload = {"data": rows}
    if isinstance(payload, Mapping):
        raw = payload.get("data", payload.get("models", payload.get("items", [])))
    else:
        raw = payload
    if not isinstance(raw, list):
        return ()
    out: list[CatalogIdentity] = []
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        identity_id = row.get("id") or row.get("name")
        if not isinstance(identity_id, str) or not identity_id.strip():
            continue
        identity_id = identity_id.strip()
        if identity_id in seen:
            continue
        seen.add(identity_id)
        owned = row.get("owned_by") or row.get("provider")
        provider = (
            owned.strip() if isinstance(owned, str) and owned.strip() else _provider_of(identity_id)
        )
        out.append(CatalogIdentity(identity_id=identity_id, provider=provider))
    return tuple(out)


def snapshot_from_payloads(
    *, catalog: object, free_tier: object, providers: object
) -> OmniRouteAdmitSnapshot:
    return OmniRouteAdmitSnapshot(
        catalog=parse_catalog_identities(catalog),
        free_tier=parse_free_tier_models(free_tier),
        connections=parse_provider_connections(providers),
    )


def normalize_omniroute_origin(base_url: str) -> str:
    url = base_url.strip()
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


def _identity_rank(identity_id: str, provider: str) -> tuple[int, int, int, str]:
    first = identity_id.split("/", 1)[0]
    opaque = 1 if is_opaque_route_id(identity_id) else 0
    combo = 1 if first in _COMBO_PREFIXES else 0
    canonical = 0 if first == provider else (1 if first in _ALIAS_PREFIXES else 2)
    return (opaque, combo, canonical, identity_id)


def _matches_free_model(catalog_id: str, model_id: str, provider: str) -> bool:
    if catalog_id == model_id:
        return True
    if catalog_id == f"{provider}/{model_id}":
        return True
    return catalog_id.endswith("/" + model_id)


def _is_positively_free_identity(identity_id: str) -> bool:
    return free_status({"id": identity_id}) == "free"


def _choose_sort(identity_id: str, active_healthy: frozenset[str]) -> tuple[int, int, int, str]:
    lowered = identity_id.lower()
    leaf = lowered.rsplit("/", 1)[-1]
    free_mark = 0 if (":free" in lowered or leaf.endswith("-free") or leaf.endswith(":free")) else 1
    small = 0 if any(token in lowered for token in _SMALL_TOKENS) else 1
    provider = _provider_of(identity_id)
    healthy = 0 if provider in active_healthy else 1
    return (free_mark, healthy, small, identity_id)


def resolve_catalog_matches(
    model_id: str, provider: str, catalog: Sequence[CatalogIdentity]
) -> tuple[str, ...]:
    """Concrete catalog identities that correspond to one free-tier row."""
    matches = [
        item.identity_id
        for item in catalog
        if item.provider == provider and _matches_free_model(item.identity_id, model_id, provider)
    ]
    if not matches:
        matches = [
            item.identity_id
            for item in catalog
            if _matches_free_model(item.identity_id, model_id, provider)
        ]
    concrete = [item for item in matches if not is_opaque_route_id(item)]
    if not concrete:
        return tuple(matches)
    concrete.sort(key=lambda identity_id: _identity_rank(identity_id, provider))
    best_rank = _identity_rank(concrete[0], provider)[:3]
    return tuple(item for item in concrete if _identity_rank(item, provider)[:3] == best_rank)


def admit_free_tier_active(snapshot: OmniRouteAdmitSnapshot) -> FreeTierAdmitReceipt:
    """Admit only free-tier ∩ active-provider concrete catalog identities."""
    active = snapshot.active_providers
    catalog = snapshot.catalog
    exclusions: list[NamedDrop] = []
    admitted: list[str] = []
    admitted_set: set[str] = set()

    def _admit(identity_id: str) -> None:
        if identity_id in admitted_set:
            return
        admitted_set.add(identity_id)
        admitted.append(identity_id)

    for row in snapshot.free_tier:
        label = f"{row.provider}/{row.model_id}"
        if is_opaque_route_id(row.model_id) or is_opaque_route_id(label):
            exclusions.append(NamedDrop(label, REASON_OPAQUE_AUTO, "opaque auto/* alias"))
            continue
        if (row.free_type or "").lower() == "discontinued":
            exclusions.append(
                NamedDrop(label, REASON_NOT_FREE_TIER, "discontinued free-tier metadata")
            )
            continue
        if row.provider not in active:
            exclusions.append(
                NamedDrop(
                    label, REASON_INACTIVE_UNCONNECTED, "provider is not an active connection"
                )
            )
            continue
        matches = resolve_catalog_matches(row.model_id, row.provider, catalog)
        concrete = [item for item in matches if not is_opaque_route_id(item)]
        if not concrete:
            if matches:
                exclusions.append(
                    NamedDrop(label, REASON_OPAQUE_AUTO, "resolved only to opaque aliases")
                )
            else:
                exclusions.append(
                    NamedDrop(
                        label,
                        REASON_METADATA_GHOST,
                        "free-tier metadata has no concrete catalog identity",
                    )
                )
            continue
        for identity_id in concrete:
            _admit(identity_id)

    free_providers = snapshot.free_tier_providers
    for identity in catalog:
        if identity.identity_id in admitted_set:
            continue
        if is_opaque_route_id(identity.identity_id):
            if identity.provider in active and identity.provider in free_providers:
                exclusions.append(
                    NamedDrop(identity.identity_id, REASON_OPAQUE_AUTO, "opaque catalog alias")
                )
            continue
        if identity.provider not in active:
            continue
        if identity.provider not in free_providers:
            continue
        if _is_positively_free_identity(identity.identity_id):
            first = identity.identity_id.split("/", 1)[0]
            if first in _COMBO_PREFIXES:
                exclusions.append(
                    NamedDrop(
                        identity.identity_id, REASON_OPAQUE_AUTO, "combo-prefixed catalog identity"
                    )
                )
                continue
            _admit(identity.identity_id)

    admitted_sorted = tuple(sorted(admitted))
    healthy = frozenset(
        row.provider
        for row in snapshot.connections
        if row.is_active and (row.test_status or "active") == "active"
    )
    chosen = None
    if admitted_sorted:
        chosen = sorted(admitted_sorted, key=lambda item: _choose_sort(item, healthy))[0]
    return FreeTierAdmitReceipt(
        admitted=admitted_sorted,
        exclusions=tuple(exclusions),
        chosen=chosen,
        empty_intersection=chosen is None,
        active_providers=tuple(sorted(active)),
        free_tier_providers=tuple(sorted(free_providers)),
    )


def load_omniroute_admit_snapshot(
    base_url: str,
    api_key: str | None = None,
    *,
    timeout: float = 20.0,
    transport: httpx.BaseTransport | None = None,
) -> OmniRouteAdmitSnapshot:
    """Fetch catalog, free-tier summary, and provider connections from OmniRoute."""
    origin = normalize_omniroute_origin(base_url)
    headers = {"accept": "application/json"}
    if api_key and api_key.strip():
        headers["authorization"] = f"Bearer {api_key.strip()}"
    paths = {
        "catalog": "/v1/models",
        "free_tier": "/api/free-tier/summary",
        "providers": "/api/providers",
    }
    payloads: dict[str, object] = {}
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            for name, path in paths.items():
                response = client.get(f"{origin}{path}", headers=headers)
                if response.status_code in {401, 403}:
                    raise LiveAdmitError("unauthorized", f"{path} returned {response.status_code}")
                if response.status_code == 404:
                    raise LiveAdmitError("unsupported", f"{path} is unavailable")
                if not 200 <= response.status_code < 300:
                    raise LiveAdmitError("http_error", f"{path} returned {response.status_code}")
                try:
                    payloads[name] = response.json()
                except ValueError as exc:
                    raise LiveAdmitError("malformed", f"{path} is not JSON") from exc
    except LiveAdmitError:
        raise
    except httpx.TimeoutException as exc:
        raise LiveAdmitError("timeout", "timed out reading OmniRoute admit surfaces") from exc
    except httpx.HTTPError as exc:
        raise LiveAdmitError("transport", type(exc).__name__) from exc
    return snapshot_from_payloads(
        catalog=payloads["catalog"],
        free_tier=payloads["free_tier"],
        providers=payloads["providers"],
    )


def execute_offload_chat(
    base_url: str,
    model_id: str,
    task: str,
    *,
    api_key: str | None = None,
    max_tokens: int = 256,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str, str]:
    """Send the routed (optionally packed) content through OmniRoute chat.

    ``task`` is the user message body — typically the cheap-path compiled
    prompt when a context pack was built. Returns
    ``(transport_outcome, content_or_error)``. Never returns secrets.
    """
    origin = normalize_omniroute_origin(base_url)
    headers = {"accept": "application/json", "content-type": "application/json"}
    if api_key and api_key.strip():
        headers["authorization"] = f"Bearer {api_key.strip()}"
    url = f"{origin}/v1/chat/completions"
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": task}],
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False) as client:
            response = client.post(url, headers=headers, json=payload)
            if not 200 <= response.status_code < 300:
                return "error", f"http {response.status_code}"
            body = response.json()
    except httpx.TimeoutException:
        return "error", "timeout"
    except (httpx.HTTPError, ValueError) as exc:
        return "error", type(exc).__name__
    choices = body.get("choices") if isinstance(body, Mapping) else None
    if not isinstance(choices, list) or not choices:
        return "sent", ""
    message = (choices[0] or {}).get("message") if isinstance(choices[0], Mapping) else {}
    content = message.get("content") if isinstance(message, Mapping) else ""
    return "sent", str(content or "")


def omniroute_endpoint_from_env(
    providers: Mapping[str, Any] | None = None,
) -> tuple[str, str | None] | None:
    """Return ``(base_url, api_key)`` for OmniRoute when configured."""
    env_url = os.getenv("OMNIROUTE_BASE_URL")
    env_key = os.getenv("OMNIROUTE_API_KEY")
    if env_url and env_url.strip():
        return env_url.strip(), env_key
    if not providers:
        return None
    for name, cfg in providers.items():
        if name != "omniroute":
            continue
        base_url = getattr(cfg, "base_url", "") or ""
        if not str(base_url).strip():
            return None
        key = getattr(cfg, "api_key", None)
        env_name = getattr(cfg, "api_key_env", None)
        if not key and env_name:
            key = os.getenv(str(env_name))
        return str(base_url).strip(), key or env_key
    return None


__all__ = [
    "FAIL_CLOSED_REASON",
    "NO_ELIGIBLE_TARGET",
    "REASON_INACTIVE_UNCONNECTED",
    "REASON_METADATA_GHOST",
    "REASON_NOT_FREE_TIER",
    "REASON_OPAQUE_AUTO",
    "CatalogIdentity",
    "CheapPathContextPack",
    "FreeTierAdmitReceipt",
    "FreeTierModel",
    "LiveAdmitError",
    "NamedDrop",
    "NamedOmission",
    "OmniRouteAdmitSnapshot",
    "ProviderConnection",
    "admit_free_tier_active",
    "build_cheap_path_context_pack",
    "execute_offload_chat",
    "load_omniroute_admit_snapshot",
    "normalize_omniroute_origin",
    "omniroute_endpoint_from_env",
    "parse_catalog_identities",
    "parse_free_tier_models",
    "parse_provider_connections",
    "snapshot_from_payloads",
]
