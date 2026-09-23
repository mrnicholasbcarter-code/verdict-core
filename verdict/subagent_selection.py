"""Launch-time model selection for Prime/RLM Verdict workers.

OmniRoute inventory is discovery only.  A route is spawnable only when it is
also visible in Prime's registry and has a fresh, cached inference probe.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CONTROLLER_MODEL = "cx/gpt-5.6-sol"
DEFAULT_OMNIROUTE_URL = "http://127.0.0.1:20128/v1"


@dataclass(frozen=True)
class WorkerTask:
    """Capabilities and capacity required by one worker spawn."""

    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    min_context_tokens: int = 0
    coding: bool = False
    reasoning: bool = False
    protected: bool = False
    frontier_worthy: bool = False

    @property
    def allow_frontier(self) -> bool:
        return self.protected or self.frontier_worthy


@dataclass(frozen=True)
class LaunchCandidate:
    """A concrete OmniRoute route that Prime can spawn."""

    route_id: str
    selector: str
    capabilities: frozenset[str]
    context_tokens: int
    input_cost: float
    output_cost: float
    is_free: bool
    is_frontier: bool
    coding_score: int
    reasoning_score: int


@dataclass(frozen=True)
class HealthResult:
    """Sanitized result of a one-token inference check."""

    healthy: bool
    category: str
    status_code: int | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class SelectionResult:
    candidate: LaunchCandidate
    checked: tuple[tuple[str, str], ...]

    @property
    def model(self) -> str:
        """Exact value that must be passed as ``model=`` to ``rlm.spawn``."""
        return self.candidate.selector


class NoHealthyWorkerModelError(RuntimeError):
    """No route satisfies inventory, registry, capability, and health gates."""


class HealthCache:
    """Small persistent probe cache with status-specific retry deadlines."""

    def __init__(self, path: Path | None = None, *, healthy_ttl_seconds: float = 300.0) -> None:
        self.path = path or Path.home() / ".verdict" / "subagent-health.json"
        self.healthy_ttl_seconds = healthy_ttl_seconds
        self._records = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._records, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        temporary.replace(self.path)

    def usable(self, selector: str, *, now: datetime) -> HealthResult | None:
        raw = self._records.get(selector)
        if not isinstance(raw, dict):
            return None
        try:
            expires = datetime.fromisoformat(str(raw["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            return None
        if expires <= now:
            return None
        return HealthResult(
            healthy=raw.get("healthy") is True,
            category=str(raw.get("category", "unknown")),
            status_code=raw.get("status_code") if type(raw.get("status_code")) is int else None,
            retry_after_seconds=None,
        )

    def record(self, selector: str, result: HealthResult, *, now: datetime) -> None:
        ttl = self.healthy_ttl_seconds if result.healthy else _failure_cooldown(result)
        self._records[selector] = {
            "healthy": result.healthy,
            "category": result.category,
            "status_code": result.status_code,
            "observed_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=ttl)),
        }
        self._save()


def classify_probe_status(
    status_code: int | None,
    *, timed_out: bool = False,
    retry_after_seconds: float | None = None,
) -> HealthResult:
    """Classify launch health without conflating auth, payment, or permission."""
    if timed_out:
        return HealthResult(False, "timeout", retry_after_seconds=retry_after_seconds)
    if status_code is not None and 200 <= status_code < 300:
        return HealthResult(True, "healthy", status_code)
    categories = {
        400: "unsupported",
        401: "authentication",
        402: "payment_required",
        403: "permission",
        429: "rate_limited",
    }
    if status_code in categories:
        return HealthResult(False, categories[status_code], status_code, retry_after_seconds)
    if status_code is not None and status_code >= 500:
        return HealthResult(False, "upstream_temporary", status_code, retry_after_seconds)
    return HealthResult(False, "transport_temporary", status_code, retry_after_seconds)


def _failure_cooldown(result: HealthResult) -> float:
    if result.retry_after_seconds is not None and result.retry_after_seconds >= 0:
        return min(result.retry_after_seconds, 86_400.0)
    return {
        "unsupported": 86_400.0,
        "authentication": 3_600.0,
        "payment_required": 3_600.0,
        "permission": 3_600.0,
        "rate_limited": 60.0,
        "timeout": 60.0,
        "upstream_temporary": 60.0,
        "transport_temporary": 60.0,
    }.get(result.category, 60.0)


def candidates_from_inventory(
    rows: Iterable[Mapping[str, Any]], prime_selectors: Iterable[str]
) -> tuple[LaunchCandidate, ...]:
    """Intersect OmniRoute live inventory with separately observed Prime visibility."""
    visible = {item.strip() for item in prime_selectors if isinstance(item, str) and item.strip()}
    result: list[LaunchCandidate] = []
    for row in rows:
        route_id = row.get("id")
        if not isinstance(route_id, str) or not route_id.strip():
            continue
        selector = f"omniroute/{route_id}"
        if selector == CONTROLLER_MODEL or selector not in visible or _opaque(route_id):
            continue
        raw_capabilities = row.get("capabilities")
        capabilities = frozenset(
            str(key)
            for key, value in raw_capabilities.items()
            if isinstance(raw_capabilities, Mapping) and value is True
        ) if isinstance(raw_capabilities, Mapping) else frozenset()
        if "tool_calling" in capabilities:
            capabilities = capabilities | {"tools"}
        raw_pricing = row.get("pricing")
        pricing: Mapping[str, Any] = raw_pricing if isinstance(raw_pricing, Mapping) else {}
        input_cost = _cost(pricing.get("input"))
        output_cost = _cost(pricing.get("output"))
        is_free = input_cost == 0.0 and output_cost == 0.0 and bool(pricing)
        lowered = route_id.lower()
        frontier = any(
            token in lowered
            for token in ("opus", "gpt-5.6", "gpt-5.5-pro", "gpt-5.4-pro", "frontier")
        )
        result.append(
            LaunchCandidate(
                route_id=route_id,
                selector=selector,
                capabilities=capabilities,
                context_tokens=_positive_int(
                    row.get("max_input_tokens"), row.get("context_length"), default=-1
                ),
                input_cost=input_cost,
                output_cost=output_cost,
                is_free=is_free,
                is_frontier=frontier,
                coding_score=sum(
                    token in lowered
                    for token in ("code", "coder", "codex", "devstral", "codestral")
                ),
                reasoning_score=int(
                    "reasoning" in capabilities
                    or "thinking" in capabilities
                    or any(token in lowered for token in ("reason", "thinking", "r1"))
                ),
            )
        )
    return tuple(result)


def select_worker_model(
    task: WorkerTask,
    *,
    inventory_rows: Iterable[Mapping[str, Any]],
    prime_selectors: Iterable[str],
    probe: Callable[[LaunchCandidate], HealthResult],
    cache: HealthCache,
    now: datetime | None = None,
    max_probes: int = 12,
) -> SelectionResult:
    """Return the cheapest qualified, currently healthy explicit spawn target."""
    current = now or datetime.now(timezone.utc)
    required = set(task.required_capabilities)
    if task.reasoning:
        required.add("reasoning")
    candidates = [
        item
        for item in candidates_from_inventory(inventory_rows, prime_selectors)
        if required <= item.capabilities
        and item.context_tokens >= task.min_context_tokens
        and (task.allow_frontier or not item.is_frontier)
    ]
    candidates.sort(key=lambda item: _rank_key(item, task))
    checked: list[tuple[str, str]] = []
    probes = 0
    for candidate in candidates:
        health = cache.usable(candidate.selector, now=current)
        if health is None:
            if probes >= max_probes:
                break
            probes += 1
            try:
                health = probe(candidate)
            except TimeoutError:
                health = classify_probe_status(None, timed_out=True)
            except (OSError, ValueError):
                health = classify_probe_status(None)
            cache.record(candidate.selector, health, now=current)
        checked.append((candidate.selector, health.category))
        if health.healthy:
            return SelectionResult(candidate, tuple(checked))
    detail = ", ".join(f"{model}:{state}" for model, state in checked)
    raise NoHealthyWorkerModelError(f"no healthy eligible Prime-visible OmniRoute worker; {detail}")


def _rank_key(candidate: LaunchCandidate, task: WorkerTask) -> tuple[Any, ...]:
    # Free always wins. Within the same cost class prefer suitability, then cost.
    suitability = candidate.coding_score if task.coding else candidate.reasoning_score
    return (
        0 if candidate.is_free else 1,
        -suitability,
        candidate.input_cost + candidate.output_cost,
        1 if candidate.is_frontier else 0,
        candidate.selector,
    )


def openai_health_probe(
    base_url: str = DEFAULT_OMNIROUTE_URL,
    *,
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Callable[[LaunchCandidate], HealthResult]:
    """Build a privacy-safe one-output-token inference probe."""
    endpoint = base_url.rstrip("/") + "/chat/completions"

    def probe(candidate: LaunchCandidate) -> HealthResult:
        body = json.dumps(
            {
                "model": candidate.route_id,
                "messages": [{"role": "user", "content": "Return exactly: OK"}],
                "max_tokens": 1,
                "stream": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with opener(request, timeout=timeout_seconds) as response:
                raw = response.read(65_537)
                if len(raw) > 65_536:
                    return HealthResult(False, "malformed_response", response.status)
                try:
                    payload = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    return HealthResult(False, "malformed_response", response.status)
                if not _has_inference_output(payload):
                    return HealthResult(False, "malformed_response", response.status)
                return classify_probe_status(response.status)
        except urllib.error.HTTPError as exc:
            return classify_probe_status(
                exc.code, retry_after_seconds=_retry_after(exc.headers.get("Retry-After"))
            )
        except TimeoutError:
            return classify_probe_status(None, timed_out=True)
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return classify_probe_status(None, timed_out=True)
            return classify_probe_status(None)

    return probe


def _has_inference_output(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    choices = payload.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        return False
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return True
    return False


def fetch_omniroute_inventory(
    base_url: str = DEFAULT_OMNIROUTE_URL, *, timeout_seconds: float = 10.0
) -> tuple[Mapping[str, Any], ...]:
    """Fetch discovery rows. The result is not availability evidence."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        raw = json.loads(response.read(16_777_217))
    data = raw.get("data") if isinstance(raw, Mapping) else None
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
        raise ValueError("OmniRoute /models response has no data array")
    return tuple(item for item in data if isinstance(item, Mapping))


def _opaque(route_id: str) -> bool:
    lowered = route_id.strip().lower()
    return lowered in {"auto", "default", "best"} or lowered.startswith(
        ("auto/", "combo/", "router/", "virtual/")
    )


def _cost(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return float("inf")
    return float(value)


def _positive_int(*values: Any, default: int) -> int:
    for value in values:
        if type(value) is int and value > 0:
            return value
    return default


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
