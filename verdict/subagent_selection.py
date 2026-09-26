"""Launch-time model selection for Prime/RLM Verdict workers.

OmniRoute inventory is discovery only.  A route is spawnable only when it is
also visible in Prime's registry and has a fresh, cached inference probe.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

CONTROLLER_MODEL = "cx/gpt-5.6-sol"
CONTROLLER_MODELS = frozenset({CONTROLLER_MODEL, "cx/gpt-6-astra"})
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


@dataclass(frozen=True)
class WorkerExecutionResult:
    """A completed worker operation and the isolated attempts that preceded it."""

    value: str
    candidate: LaunchCandidate
    attempts: tuple[tuple[str, str], ...]


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
        for key in (_provider_cache_key(selector), selector):
            result = self._usable_key(key, now=now)
            if result is not None:
                return result
        return None

    def _usable_key(self, key: str, *, now: datetime) -> HealthResult | None:
        raw = self._records.get(key)
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
        self._record_key(selector, result, now=now)
        self._save()

    def record_failure(
        self, candidate: LaunchCandidate, result: HealthResult, *, now: datetime
    ) -> None:
        """Persist both candidate and provider cooldowns for a runtime failure."""
        self._record_key(candidate.selector, result, now=now)
        if result.category == "rate_limited":
            self._record_key(_provider_cache_key(candidate.selector), result, now=now)
        self._save()

    def _record_key(self, key: str, result: HealthResult, *, now: datetime) -> None:
        ttl = self.healthy_ttl_seconds if result.healthy else _failure_cooldown(result)
        self._records[key] = {
            "healthy": result.healthy,
            "category": result.category,
            "status_code": result.status_code,
            "observed_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=ttl)),
        }


_UNSERVABLE_MARKERS = ("not available in the active live catalog",)


def classify_probe_status(
    status_code: int | None,
    *,
    timed_out: bool = False,
    retry_after_seconds: float | None = None,
    body: str = "",
) -> HealthResult:
    """Classify launch health without conflating auth, payment, or permission.

    ``body`` is the (bounded) error response text. A 400 whose body says the
    advertised id is not in the gateway's live catalog is ``unservable``: the
    gateway lists the route but cannot serve it.
    """
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
    if status_code == 400 and any(m in body.lower() for m in _UNSERVABLE_MARKERS):
        return HealthResult(False, "unservable", status_code, retry_after_seconds)
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
        if route_id in CONTROLLER_MODELS or selector not in visible or _opaque(route_id):
            continue
        raw_capabilities = row.get("capabilities")
        capabilities = (
            frozenset(
                str(key)
                for key, value in raw_capabilities.items()
                if isinstance(raw_capabilities, Mapping) and value is True
            )
            if isinstance(raw_capabilities, Mapping)
            else frozenset()
        )
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
            for token in (
                "opus",
                "gpt-5.6",
                "gpt-6-sol",
                "gpt-6-astra",
                "gpt-5.5-pro",
                "gpt-5.4-pro",
                "frontier",
            )
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
    return tuple({item.selector: item for item in result}.values())


def eligible_worker_candidates(
    task: WorkerTask, inventory_rows: Iterable[Mapping[str, Any]], prime_selectors: Iterable[str]
) -> tuple[LaunchCandidate, ...]:
    """Rank the entire unique eligible pool; never truncate a discovery prefix."""
    required = set(task.required_capabilities)
    if task.reasoning:
        required.add("reasoning")
    return tuple(
        sorted(
            (
                item
                for item in candidates_from_inventory(inventory_rows, prime_selectors)
                if required <= item.capabilities
                and item.context_tokens >= task.min_context_tokens
                and (task.allow_frontier or not item.is_frontier)
            ),
            key=lambda item: _rank_key(item, task),
        )
    )


def select_worker_model(
    task: WorkerTask,
    *,
    inventory_rows: Iterable[Mapping[str, Any]],
    prime_selectors: Iterable[str],
    probe: Callable[[LaunchCandidate], HealthResult],
    cache: HealthCache,
    now: datetime | None = None,
    max_probes: int | None = None,
) -> SelectionResult:
    """Return the cheapest qualified, currently healthy explicit spawn target."""
    current = now or datetime.now(timezone.utc)
    candidates = eligible_worker_candidates(task, inventory_rows, prime_selectors)
    checked: list[tuple[str, str]] = []
    probes = 0
    for candidate in candidates:
        health = cache.usable(candidate.selector, now=current)
        if health is None:
            if max_probes is not None and probes >= max_probes:
                break
            probes += 1
            try:
                health = probe(candidate)
            except TimeoutError:
                health = classify_probe_status(None, timed_out=True)
            except Exception as exc:
                health = classify_worker_failure(exc, now=current)
            cache.record(candidate.selector, health, now=current)
        checked.append((candidate.selector, health.category))
        if health.healthy:
            return SelectionResult(candidate, tuple(checked))
    detail = ", ".join(f"{model}:{state}" for model, state in checked)
    raise NoHealthyWorkerModelError(f"no healthy eligible Prime-visible OmniRoute worker; {detail}")


async def execute_with_worker_failover(
    task: WorkerTask,
    *,
    inventory_rows: Iterable[Mapping[str, Any]],
    prime_selectors: Iterable[str],
    probe: Callable[[LaunchCandidate], HealthResult],
    execute: Callable[[str], Awaitable[WorkerTerminal]],
    cache: HealthCache,
    now: Callable[[], datetime] | None = None,
    max_replacements: int | None = None,
    total_timeout_seconds: float = 900,
    attempt_timeout_seconds: float = 180,
) -> WorkerExecutionResult:
    """Compatibility entry point; execute must return a completed terminal envelope.

    Admission handles and arbitrary callback values are failures, never success.
    Production RLM dispatch uses WorkerController with an owned Prime adapter.
    """
    from verdict.worker_runtime import CallbackAdapter, RuntimeBudget, WorkerController

    if max_replacements is not None and max_replacements < 0:
        raise ValueError("max_replacements must be non-negative")
    controller = WorkerController(
        task,
        inventory_rows=inventory_rows,
        prime_selectors=prime_selectors,
        probe=probe,
        adapter=CallbackAdapter(execute),
        cache=cache,
        now=now,
        budget=RuntimeBudget(
            total_seconds=total_timeout_seconds,
            attempt_seconds=attempt_timeout_seconds,
            max_attempts=None if max_replacements is None else max_replacements + 1,
        ),
    )
    outcome = await controller.run("callback task")
    if outcome.state != "SUCCESS" or outcome.candidate is None:
        raise NoHealthyWorkerModelError(outcome.diagnostic)
    return WorkerExecutionResult(outcome.output, outcome.candidate, tuple(controller.attempts))


@dataclass(frozen=True)
class WorkerTerminal:
    """Full terminal assistant result, not a roster preview or admission handle."""

    state: str
    output: str | None = None
    replied: bool = False
    error: str | None = None
    stop_reason: str | None = None


def classify_worker_failure(exc: BaseException, *, now: datetime | None = None) -> HealthResult:
    """Map runtime/client exceptions onto the same policy used by health probes."""
    status = _exception_status(exc)
    retry_after = _exception_retry_after(exc, now=now)
    timed_out = isinstance(exc, (TimeoutError, socket.timeout))
    if status is None and _malformed_exception(exc):
        return HealthResult(False, "malformed_response", retry_after_seconds=retry_after)
    if status is None and not timed_out and not isinstance(exc, OSError):
        return HealthResult(False, "worker_exception")
    result = classify_probe_status(status, timed_out=timed_out, retry_after_seconds=retry_after)
    return result if not result.healthy else HealthResult(False, "worker_exception", status)


def _exception_status(exc: BaseException) -> int | None:
    for name in ("status_code", "status", "code"):
        value = getattr(exc, name, None)
        if type(value) is int and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    if type(value) is int and 100 <= value <= 599:
        return value
    match = re.search(r"(?<!\d)(400|401|402|403|429|5\d\d)(?!\d)", str(exc))
    return int(match.group(1)) if match else None


def _exception_headers(exc: BaseException) -> Mapping[str, Any]:
    for source in (exc, getattr(exc, "response", None)):
        headers = getattr(source, "headers", None)
        if isinstance(headers, Mapping):
            return headers
    return {}


def _exception_retry_after(exc: BaseException, *, now: datetime | None) -> float | None:
    headers = {str(key).lower(): value for key, value in _exception_headers(exc).items()}
    current = now or datetime.now(timezone.utc)
    for key in ("retry-after", "x-ratelimit-reset", "x-rate-limit-reset", "ratelimit-reset"):
        value = headers.get(key)
        parsed = _retry_delay(value, now=current, reset=key != "retry-after")
        if parsed is not None:
            return parsed
    message = str(exc)
    retry_match = re.search(r"retry[-_ ]?after[\s:=\"']+(\d+(?:\.\d+)?)", message, re.IGNORECASE)
    if retry_match:
        return float(retry_match.group(1))
    reset_match = re.search(
        r"(?:x[-_ ]?)?rate[-_ ]?limit[-_ ]?reset[\s:=\"']+(\d+(?:\.\d+)?)", message, re.IGNORECASE
    )
    return _retry_delay(reset_match.group(1), now=current, reset=True) if reset_match else None


def _malformed_exception(exc: BaseException) -> bool:
    return isinstance(exc, (ValueError, TypeError, json.JSONDecodeError)) or any(
        token in str(exc).lower() for token in ("malformed", "invalid response", "decode")
    )


# Economic ranking policy (NOT a fallback chain): eligibility, health and task
# fit always gate first; this only orders the surviving pool.  Subscription
# capacity is already paid for, so it outranks free-tier and metered capacity.
# Operators override the provider->class map with VERDICT_CAPACITY_CLASSES,
# e.g. "cc=claude_subscription,cx=subscription".
CAPACITY_CLASS_ORDER: tuple[str, ...] = ("claude_subscription", "subscription", "free", "metered")
DEFAULT_PROVIDER_CAPACITY_CLASS: Mapping[str, str] = {
    "cc": "claude_subscription",
    "cx": "subscription",
}


def provider_capacity_classes() -> dict[str, str]:
    """Provider-prefix -> capacity class, with an operator env override."""
    import os

    mapping = dict(DEFAULT_PROVIDER_CAPACITY_CLASS)
    raw = os.environ.get("VERDICT_CAPACITY_CLASSES", "")
    for item in raw.split(","):
        if "=" not in item:
            continue
        provider, _, klass = item.partition("=")
        provider, klass = provider.strip().lower(), klass.strip()
        if provider and klass in CAPACITY_CLASS_ORDER:
            mapping[provider] = klass
    return mapping


def capacity_class(candidate: LaunchCandidate) -> str:
    """Economic class of a candidate: subscription, free, or metered."""
    provider = candidate.route_id.split("/", 1)[0].strip().lower()
    mapped = provider_capacity_classes().get(provider)
    if mapped is not None:
        return mapped
    return "free" if candidate.is_free else "metered"


def _rank_key(candidate: LaunchCandidate, task: WorkerTask) -> tuple[Any, ...]:
    # Capacity class first (subscription before free before metered), then task
    # suitability, then marginal cost.  Frontier routes only reach this point
    # when the task is frontier-worthy; otherwise they were filtered out.
    suitability = candidate.coding_score if task.coding else candidate.reasoning_score
    return (
        CAPACITY_CLASS_ORDER.index(capacity_class(candidate)),
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
            try:
                detail = exc.read(4096).decode("utf-8", "replace")
            except Exception:  # body is diagnostic only; never fail the probe on it
                detail = ""
            return classify_probe_status(
                exc.code,
                retry_after_seconds=_retry_after_headers(
                    exc.headers, now=datetime.now(timezone.utc)
                ),
                body=detail,
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
    url = base_url.rstrip("/") + "/models"
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError(f"OmniRoute base URL must be http(s): {base_url!r}")
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    # Scheme validated above; the gateway URL is operator configuration.
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
        raw = json.loads(response.read(16_777_217))
    data = raw.get("data") if isinstance(raw, Mapping) else None
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
        raise ValueError("OmniRoute /models response has no data array")
    return tuple(item for item in data if isinstance(item, Mapping))


def _provider_cache_key(selector: str) -> str:
    route = selector.removeprefix("omniroute/")
    provider = route.split("/", 1)[0].strip().lower()
    return f"provider:{provider}" if provider else f"provider:{selector.lower()}"


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


def _retry_after_headers(headers: Any, *, now: datetime) -> float | None:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for key in ("retry-after", "x-ratelimit-reset", "x-rate-limit-reset", "ratelimit-reset"):
        result = _retry_delay(lowered.get(key), now=now, reset=key != "retry-after")
        if result is not None:
            return result
    return None


def _retry_delay(value: Any, *, now: datetime, reset: bool = False) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        if reset:
            return None
        try:
            target = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, (target - now).total_seconds())
    if reset and number >= 1_000_000_000_000:
        number /= 1000.0
    if reset and number >= 1_000_000_000:
        return max(0.0, number - now.timestamp())
    return max(0.0, number)


def _retry_after(value: str | None) -> float | None:
    """Compatibility helper for delta-seconds Retry-After values."""
    return _retry_delay(value, now=datetime.now(timezone.utc))


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
