"""Credential-safe HTTP transport for documented OmniRoute availability reads."""

from __future__ import annotations

import ipaddress
import json
import math
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from llm_gate.availability import (
    OmniRouteTransportError,
    OmniRouteTransportMalformed,
    OmniRouteTransportTimeout,
    OmniRouteTransportUnauthorized,
    OmniRouteTransportUnsupported,
)
from llm_gate.security import host_is_allowed, validate_upstream_url

_RUNTIME_SOURCE_PATHS = {
    "health": "/api/monitoring/health",
    "rate_limits": "/api/rate-limits",
    "model_cooldowns": "/api/resilience/model-cooldowns",
    "budget": "/api/usage/budget",
    "token_limits": "/api/usage/token-limits",
}
_MANAGEMENT_SOURCES = frozenset({"rate_limits", "model_cooldowns", "budget", "token_limits"})
_USAGE_SCOPED_SOURCES = frozenset({"budget", "token_limits"})
_DEFAULT_RUNTIME_SOURCES = frozenset({"health"})
_MAX_JSON_CONTAINER_DEPTH = 64


class OmniRouteHTTPTransport:
    """Fetch a bounded allowlist of documented OmniRoute JSON operations.

    Catalog and management credentials are deliberately separate. Response
    bodies and upstream error text are never included in raised exceptions.
    """

    configured_operations = frozenset({"catalog", "runtime"})

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        management_token: str | None = None,
        usage_api_key_id: str | None = None,
        runtime_sources: set[str] | frozenset[str] = _DEFAULT_RUNTIME_SOURCES,
        timeout: float = 5.0,
        max_response_bytes: int = 1_048_576,
        ttl_seconds: int = 30,
        allow_private_hosts: set[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.allow_private_hosts = {
            host.rstrip(".").lower() for host in (allow_private_hosts or set())
        }
        normalized = validate_upstream_url(base_url, allow_private_hosts=self.allow_private_hosts)
        parsed = urlsplit(normalized)
        host = (parsed.hostname or "").rstrip(".").lower()
        if host not in self.allow_private_hosts:
            raise ValueError(
                "OmniRoute destination host must be explicitly present in the allowlist"
            )
        if parsed.scheme == "http":
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                raise ValueError(
                    "plain HTTP OmniRoute destinations must use an IP literal"
                ) from None
            if not address.is_loopback:
                raise ValueError("plain HTTP OmniRoute destinations must use a loopback IP literal")
        path = parsed.path.rstrip("/")
        if path not in {"", "/v1"}:
            raise ValueError("OmniRoute base URL path must be empty or /v1")
        self.base_url = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout))
            or not 0 < float(timeout) <= 60
        ):
            raise ValueError("timeout must be a finite number in the range (0, 60]")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 16_777_216:
            raise ValueError("max_response_bytes must be an integer between 1 and 16777216")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 3_600:
            raise ValueError("ttl_seconds must be an integer between 1 and 3600")
        if not isinstance(runtime_sources, (set, frozenset)):
            raise ValueError("runtime_sources must be a set of documented source names")
        unknown_sources = set(runtime_sources) - set(_RUNTIME_SOURCE_PATHS)
        if unknown_sources:
            raise ValueError(f"unsupported runtime source: {sorted(unknown_sources)[0]}")
        if set(runtime_sources) & _MANAGEMENT_SOURCES and not (
            management_token and management_token.strip()
        ):
            raise ValueError(
                "management_token is required for configured management runtime sources"
            )
        if set(runtime_sources) & _USAGE_SCOPED_SOURCES and not (
            usage_api_key_id and usage_api_key_id.strip()
        ):
            raise ValueError("usage_api_key_id is required for budget and token-limit sources")

        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.management_token = (
            management_token.strip() if management_token and management_token.strip() else None
        )
        self.usage_api_key_id = (
            usage_api_key_id.strip() if usage_api_key_id and usage_api_key_id.strip() else None
        )
        self.runtime_sources = frozenset(runtime_sources)
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self.ttl_seconds = ttl_seconds
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def catalog(self) -> Any:
        """Return the OpenAI-compatible catalog from ``GET /v1/models``."""
        return self._get_json(
            operation="catalog",
            path="/v1/models",
            token=self.api_key,
        )

    def runtime(self) -> dict[str, dict[str, Any]]:
        """Return minimized candidate observations from configured read sources."""
        received_at = _utc(self.clock())
        observations: dict[str, dict[str, Any]] = {}
        for source, path in _RUNTIME_SOURCE_PATHS.items():
            if source not in self.runtime_sources:
                continue
            params = (
                {"apiKeyId": self.usage_api_key_id}
                if source in _USAGE_SCOPED_SOURCES and self.usage_api_key_id
                else None
            )
            payload = self._get_json(
                operation="runtime",
                path=path,
                token=self.management_token if source in _MANAGEMENT_SOURCES else None,
                params=params,
            )
            _merge_source(
                observations,
                source=source,
                payload=payload,
                received_at=received_at,
                ttl_seconds=self.ttl_seconds,
                usage_api_key_id=self.usage_api_key_id,
            )
        return _inherit_runtime_defaults(observations)

    def discover_capabilities(self) -> dict[str, list[str]]:
        """Advertise only operations and runtime reads configured on this instance."""
        return {
            "operations": sorted(self.configured_operations),
            "runtime_sources": sorted(self.runtime_sources),
        }

    def _validate_destination(self) -> None:
        host = urlsplit(self.base_url).hostname
        if host:
            host_is_allowed(host, self.allow_private_hosts)

    def _get_json(
        self,
        *,
        operation: str,
        path: str,
        token: str | None,
        params: Mapping[str, str] | None = None,
    ) -> Any:
        headers = {"accept": "application/json", "accept-encoding": "identity"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        body = bytearray()
        failure: OmniRouteTransportError | None = None
        try:
            self._validate_destination()
            with (
                httpx.Client(
                    transport=self.transport,
                    timeout=self.timeout,
                    follow_redirects=False,
                ) as client,
                client.stream(
                    "GET",
                    f"{self.base_url}{path}",
                    headers=headers,
                    params=params,
                ) as response,
            ):
                if response.status_code in {401, 403}:
                    raise OmniRouteTransportUnauthorized(operation, "unauthorized")
                if response.status_code == 404:
                    raise OmniRouteTransportUnsupported(
                        operation, "documented operation unavailable"
                    )
                if not 200 <= response.status_code < 300:
                    raise OmniRouteTransportError(operation, f"http status {response.status_code}")
                content_encoding = (
                    response.headers.get("content-encoding", "identity").strip().lower()
                )
                if content_encoding not in {"", "identity"}:
                    raise OmniRouteTransportMalformed(
                        operation, "encoded responses are unsupported"
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    invalid_content_length = False
                    try:
                        parsed_content_length = int(content_length)
                    except ValueError:
                        invalid_content_length = True
                        parsed_content_length = 0
                    if invalid_content_length:
                        raise OmniRouteTransportMalformed(
                            operation, "invalid content length"
                        ) from None
                    if parsed_content_length > self.max_response_bytes:
                        raise OmniRouteTransportMalformed(operation, "response too large")
                chunks = (response.content,) if response.is_stream_consumed else response.iter_raw()
                for chunk in chunks:
                    body.extend(chunk)
                    if len(body) > self.max_response_bytes:
                        raise OmniRouteTransportMalformed(operation, "response too large")
        except OmniRouteTransportError as exc:
            failure = exc
        except httpx.TimeoutException:
            failure = OmniRouteTransportTimeout(operation, "timeout")
        except httpx.HTTPError as exc:
            failure = OmniRouteTransportError(operation, type(exc).__name__)
        except ValueError:
            failure = OmniRouteTransportError(operation, "destination unavailable")
        except Exception as exc:
            failure = OmniRouteTransportError(operation, type(exc).__name__)

        if failure is not None:
            raise failure from None

        malformed = False
        try:
            payload = json.loads(body)
        except (RecursionError, UnicodeDecodeError, ValueError):
            malformed = True
            payload = None
        if malformed:
            raise OmniRouteTransportMalformed(operation, "invalid JSON") from None
        if not isinstance(payload, (dict, list)):
            raise OmniRouteTransportMalformed(operation, "JSON payload must be an object or array")
        if not _json_depth_is_bounded(payload):
            raise OmniRouteTransportMalformed(operation, "JSON nesting limit exceeded")
        return payload


def _json_depth_is_bounded(payload: dict[str, Any] | list[Any]) -> bool:
    """Apply one parser-independent nesting limit to untrusted JSON."""
    stack: list[tuple[dict[str, Any] | list[Any], int]] = [(payload, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > _MAX_JSON_CONTAINER_DEPTH:
            return False
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))
    return True


def _utc(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _timestamp(payload: Any, received_at: datetime) -> str:
    if isinstance(payload, Mapping):
        value = payload.get("timestamp", payload.get("observed_at"))
        if isinstance(value, str) and value.strip():
            return value
    return received_at.isoformat()


def _base_observation(payload: Any, received_at: datetime, ttl_seconds: int) -> dict[str, Any]:
    return {
        "observed_at": _timestamp(payload, received_at),
        "ttl_seconds": ttl_seconds,
        "source": "omniroute:http",
        "health": "unknown",
        "auth": "unknown",
    }


def _health(value: Any) -> str:
    state = str(value or "").strip().lower().replace("-", "_")
    if state in {"healthy", "ok", "ready", "closed", "active", "available"}:
        return "healthy"
    if state in {"degraded", "warning", "half_open", "recovering"}:
        return "degraded"
    if state in {"rate_limited", "quota_exhausted", "locked_out", "circuit_open"}:
        return state
    if state in {"unhealthy", "down", "offline", "failed", "error", "open"}:
        return "unavailable"
    return "unknown"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _merge_observation(
    observations: dict[str, dict[str, Any]],
    key: str,
    update: Mapping[str, Any],
    base: Mapping[str, Any],
) -> None:
    if not key or len(key) > 512:
        return
    target = observations.setdefault(key, dict(base))
    for field, value in update.items():
        if value is None:
            continue
        if (
            field
            in {
                "quota_remaining_pct",
                "budget_remaining",
                "token_headroom",
            }
            and field in target
        ):
            target[field] = min(target[field], value)
        elif field == "health" and field in target:
            target[field] = _stronger_health(str(target[field]), str(value))
        elif field == "eligible" and value is False:
            target[field] = False
        elif field == "circuit" and value == "open":
            target[field] = "open"
        else:
            target[field] = value


def _stronger_health(left: str, right: str) -> str:
    severity = {
        "unknown": 0,
        "healthy": 1,
        "degraded": 2,
        "rate_limited": 3,
        "quota_exhausted": 4,
        "unavailable": 5,
        "locked_out": 6,
        "circuit_open": 7,
    }
    return right if severity.get(right, 5) > severity.get(left, 5) else left


def _records(payload: Any, *keys: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            return [item for item in value.values() if isinstance(item, Mapping)]
    return []


def _merge_source(
    observations: dict[str, dict[str, Any]],
    *,
    source: str,
    payload: Any,
    received_at: datetime,
    ttl_seconds: int,
    usage_api_key_id: str | None,
) -> None:
    base = _base_observation(payload, received_at, ttl_seconds)
    observations.setdefault("default", dict(base))
    if source == "health":
        _merge_health(observations, payload, base, received_at=received_at)
    elif source == "rate_limits":
        _merge_rate_limits(observations, payload, base, received_at=received_at)
    elif source == "model_cooldowns":
        _merge_cooldowns(observations, payload, base, received_at=received_at)
    elif source == "budget":
        _merge_budget(observations, payload, base)
    elif source == "token_limits":
        _merge_token_limits(observations, payload, base, usage_api_key_id=usage_api_key_id)


def _merge_health(
    observations: dict[str, dict[str, Any]],
    payload: Any,
    base: Mapping[str, Any],
    *,
    received_at: datetime,
) -> None:
    if not isinstance(payload, Mapping):
        return
    provider_health = payload.get("providerHealth")
    if isinstance(provider_health, Mapping):
        for provider, value in provider_health.items():
            if not isinstance(value, Mapping):
                continue
            _merge_observation(
                observations,
                str(provider),
                {"health": _health(value.get("state", value.get("status")))},
                base,
            )
    for record in _records(payload, "providerBreakers"):
        provider = record.get("provider")
        if not isinstance(provider, str):
            continue
        state = str(record.get("state", "")).lower().replace("-", "_")
        update: dict[str, Any] = {"health": _health(state)}
        if state == "open":
            update.update({"circuit": "open", "health": "circuit_open"})
        _merge_observation(observations, provider, update, base)
    for record in _records(payload, "lockouts"):
        _apply_lockout(observations, record, base, received_at=received_at)
    for record in _records(payload, "learnedLimits"):
        _apply_headroom(observations, record, base)


def _merge_rate_limits(
    observations: dict[str, dict[str, Any]],
    payload: Any,
    base: Mapping[str, Any],
    *,
    received_at: datetime,
) -> None:
    for record in _records(payload, "lockouts"):
        _apply_lockout(observations, record, base, received_at=received_at)


def _apply_headroom(
    observations: dict[str, dict[str, Any]],
    record: Mapping[str, Any],
    base: Mapping[str, Any],
) -> None:
    key = record.get("model") or record.get("provider")
    if not isinstance(key, str):
        return
    remaining = _number(record.get("remaining"))
    limit = _number(record.get("limit"))
    update: dict[str, Any] = {"health": _health(record.get("status", record.get("state")))}
    if remaining is not None and limit is not None and limit > 0:
        update["quota_remaining_pct"] = max(0.0, min(100.0, remaining / limit * 100))
        if remaining <= 0:
            update["health"] = "quota_exhausted"
    cooldown = record.get("rateLimitedUntil", record.get("cooldownUntil", record.get("resetAt")))
    if isinstance(cooldown, str):
        update["cooldown_until"] = cooldown
    _merge_observation(observations, key, update, base)


def _merge_cooldowns(
    observations: dict[str, dict[str, Any]],
    payload: Any,
    base: Mapping[str, Any],
    *,
    received_at: datetime,
) -> None:
    for record in _records(payload, "items", "lockouts", "cooldowns", "data"):
        _apply_lockout(observations, record, base, received_at=received_at)


def _apply_lockout(
    observations: dict[str, dict[str, Any]],
    record: Mapping[str, Any],
    base: Mapping[str, Any],
    *,
    received_at: datetime,
) -> None:
    provider = record.get("provider")
    model = record.get("model")
    if isinstance(model, str) and model:
        key = (
            f"{provider}/{model}"
            if isinstance(provider, str) and provider and "/" not in model
            else model
        )
    elif isinstance(provider, str) and provider:
        key = provider
    else:
        return
    until = record.get(
        "lockoutUntil",
        record.get("cooldownUntil", record.get("expiresAt", record.get("until"))),
    )
    remaining_ms = _integer(record.get("remainingMs"))
    if remaining_ms is not None:
        if remaining_ms <= 0:
            return
        until = (received_at + timedelta(milliseconds=remaining_ms)).isoformat()
    update: dict[str, Any] = {"health": "locked_out", "eligible": False}
    if isinstance(until, str):
        update["lockout_until"] = until
    _merge_observation(observations, key, update, base)


def _merge_budget(
    observations: dict[str, dict[str, Any]],
    payload: Any,
    base: Mapping[str, Any],
) -> None:
    if not isinstance(payload, Mapping):
        return
    active_limit = _number(payload.get("activeLimitUsd"))
    if active_limit is None or active_limit <= 0:
        return
    budget_check = payload.get("budgetCheck")
    if not isinstance(budget_check, Mapping):
        return
    allowed = budget_check.get("allowed")
    if type(allowed) is not bool:
        return
    if not allowed:
        _merge_observation(
            observations,
            "default",
            {"budget_remaining": 0.0, "eligible": False},
            base,
        )
        return
    remaining = _number(budget_check.get("remaining"))
    if remaining is not None:
        _merge_observation(
            observations,
            "default",
            {"budget_remaining": remaining},
            base,
        )


def _merge_token_limits(
    observations: dict[str, dict[str, Any]],
    payload: Any,
    base: Mapping[str, Any],
    *,
    usage_api_key_id: str | None,
) -> None:
    if not isinstance(payload, Mapping) or payload.get("apiKeyId") != usage_api_key_id:
        return
    for record in _records(payload, "limits", "data", "items"):
        if record.get("enabled") is False:
            continue
        record_id = record.get("apiKeyId", record.get("keyId"))
        if record_id is not None and record_id != usage_api_key_id:
            continue
        remaining = _integer(
            record.get(
                "remaining",
                record.get("tokensRemaining", record.get("tokenHeadroom")),
            )
        )
        if remaining is None:
            continue
        scope = str(record.get("scopeType", "global")).lower()
        scope_value = record.get("scopeValue")
        key = str(scope_value) if scope in {"model", "provider"} and scope_value else "default"
        update: dict[str, Any] = {"token_headroom": remaining}
        if remaining == 0:
            update["health"] = "quota_exhausted"
        _merge_observation(observations, key, update, base)


def _inherit_runtime_defaults(
    observations: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    default = observations.get("default", {})
    result: dict[str, dict[str, Any]] = {"default": dict(default)}
    for key, observation in observations.items():
        if key == "default":
            continue
        merged = dict(default)
        if "/" in key:
            merged.update(observations.get(key.split("/", 1)[0], {}))
        merged.update(observation)
        result[key] = merged
    return result


__all__ = ["OmniRouteHTTPTransport"]
