"""Bounded worker execution against an OpenAI-compatible OmniRoute gateway.

The worker layer deliberately delegates provider selection to OmniRoute.  It
only reads the live model catalog, chooses an advertised model, and forwards a
standard chat-completions request.  No provider credentials or provider list is
embedded in this module.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from verdict.security import validate_upstream_url

_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429})
_AUTO_MODEL_ALIASES = {
    "auto": ("auto/best-coding", "auto/best-reasoning", "auto/best-free", "auto/fast"),
    "coding": ("auto/best-coding", "auto/coding:free", "auto/best-free", "auto/fast"),
    "reasoning": ("auto/best-reasoning", "auto/reasoning:free", "auto/best-free", "auto/fast"),
    "fast": ("auto/best-fast", "auto/fast", "auto/best-free"),
    "free": ("auto/best-free", "auto/coding:free", "auto/reasoning:free", "auto/fast"),
}


class WorkerError(RuntimeError):
    """A redacted worker failure safe to return in orchestration results."""


class WorkerUnavailableError(WorkerError):
    """Raised when the requested worker model cannot be selected or reached."""


@dataclass(frozen=True)
class WorkerModel:
    """The small, non-secret subset of an OmniRoute catalog row used here."""

    model_id: str
    provider: str | None = None
    owned_by: str | None = None
    capabilities: frozenset[str] = frozenset()

    @classmethod
    def from_catalog_row(cls, row: Mapping[str, Any]) -> WorkerModel | None:
        model_id = row.get("id")
        if not isinstance(model_id, str) or not model_id.strip():
            return None
        capability_values: set[str] = set()
        raw_capabilities = row.get("capabilities")
        if isinstance(raw_capabilities, Sequence) and not isinstance(
            raw_capabilities, (str, bytes)
        ):
            capability_values.update(
                value.lower() for value in raw_capabilities if isinstance(value, str)
            )
        verdict = row.get("verdict")
        profile = verdict.get("capability_profile") if isinstance(verdict, Mapping) else None
        if isinstance(profile, Mapping):
            for key, value in profile.items():
                if value is True:
                    capability_values.add(str(key).lower())
        return cls(
            model_id=model_id,
            provider=_optional_string(row.get("provider")),
            owned_by=_optional_string(row.get("owned_by")),
            capabilities=frozenset(capability_values),
        )

    def supports(self, required: frozenset[str]) -> bool:
        if not required:
            return True
        capabilities = self.capabilities
        aliases = {
            "tools": {"tools", "tool-calling", "tool_calling", "function-calling"},
            "tool-calling": {"tools", "tool-calling", "tool_calling", "function-calling"},
            "reasoning": {"reasoning"},
            "vision": {"vision", "image"},
        }
        return all(bool(capabilities & aliases.get(item, {item})) for item in required)


@dataclass(frozen=True)
class WorkerRequest:
    """One bounded worker task represented as a chat-completions request."""

    messages: list[dict[str, Any]]
    task_id: str = ""
    model: str = "auto/best-free"
    required_capabilities: frozenset[str] = frozenset()
    max_tokens: int | None = 256
    temperature: float | None = None
    stream: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self, model_id: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": self.messages,
            "stream": self.stream,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        payload.update(self.extra)
        return payload


@dataclass(frozen=True)
class WorkerResult:
    """A redacted, serializable outcome for one worker task."""

    task_id: str
    requested_model: str
    selected_model: str | None
    attempts: int
    latency_ms: float
    response: dict[str, Any] | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.response is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "requested_model": self.requested_model,
            "selected_model": self.selected_model,
            "attempts": self.attempts,
            "latency_ms": round(self.latency_ms, 3),
            "response": self.response,
            "error": self.error,
            "ok": self.ok,
        }


class OmniRouteWorkerClient:
    """Discover and execute bounded worker requests through OmniRoute."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:20128/v1",
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        max_attempts: int = 3,
        retry_backoff_seconds: float = 0.1,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_private_hosts: set[str] | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("OmniRoute base URL must not be empty")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 8:
            raise ValueError("max_attempts must be an integer between 1 and 8")
        if timeout <= 0 or timeout > 300:
            raise ValueError("timeout must be in the range (0, 300]")
        if retry_backoff_seconds < 0 or retry_backoff_seconds > 10:
            raise ValueError("retry_backoff_seconds must be in the range [0, 10]")
        private_hosts = {"127.0.0.1", "localhost", "::1"}
        private_hosts.update(host.rstrip(".").lower() for host in (allow_private_hosts or set()))
        self.base_url = validate_upstream_url(base_url, allow_private_hosts=private_hosts).rstrip(
            "/"
        )
        self.api_key = api_key.strip() if api_key and api_key.strip() else None
        self.timeout = float(timeout)
        self.max_attempts = max_attempts
        self.retry_backoff_seconds = float(retry_backoff_seconds)
        self.transport = transport
        self._catalog: tuple[WorkerModel, ...] = ()
        self._catalog_lock = asyncio.Lock()

    def _headers(self) -> dict[str, str]:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.timeout, follow_redirects=False
        ) as client:
            try:
                return await client.request(method, f"{self.base_url}/{path.lstrip('/')}", **kwargs)
            except httpx.TimeoutException as exc:
                raise WorkerUnavailableError("OmniRoute request timed out") from exc
            except httpx.HTTPError as exc:
                raise WorkerUnavailableError("OmniRoute transport unavailable") from exc

    async def discover_models(self, *, refresh: bool = False) -> tuple[WorkerModel, ...]:
        """Fetch the live catalog, retaining only safe model metadata."""
        async with self._catalog_lock:
            if self._catalog and not refresh:
                return self._catalog
            response = await self._request("GET", "models", headers=self._headers())
            if response.status_code in {401, 403}:
                raise WorkerUnavailableError("OmniRoute catalog authorization failed")
            if response.status_code >= 400:
                raise WorkerUnavailableError(
                    f"OmniRoute catalog returned HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise WorkerUnavailableError("OmniRoute catalog returned invalid JSON") from exc
            rows = payload.get("data") if isinstance(payload, Mapping) else None
            if not isinstance(rows, list):
                raise WorkerUnavailableError("OmniRoute catalog did not contain a model list")
            self._catalog = tuple(
                model
                for row in rows
                if isinstance(row, Mapping)
                for model in [WorkerModel.from_catalog_row(row)]
                if model is not None
            )
            if not self._catalog:
                raise WorkerUnavailableError("OmniRoute catalog contained no usable models")
            return self._catalog

    async def complete(self, request: WorkerRequest) -> WorkerResult:
        """Execute one request with bounded transient failover."""
        started = time.perf_counter()
        models = await self.discover_models()
        candidates = self._select_models(request, models)
        if not candidates:
            raise WorkerUnavailableError(f"no OmniRoute model satisfies {request.model!r}")

        attempts = 0
        last_error = "OmniRoute worker failed"
        attempt_models = candidates[: self.max_attempts]
        if len(attempt_models) < self.max_attempts:
            attempt_models.extend([candidates[-1]] * (self.max_attempts - len(attempt_models)))
        for model in attempt_models:
            attempts += 1
            try:
                response = await self._request(
                    "POST",
                    "chat/completions",
                    headers={**self._headers(), "content-type": "application/json"},
                    json=request.payload(model.model_id),
                )
            except WorkerUnavailableError as exc:
                last_error = str(exc)
                await self._backoff(attempts)
                continue
            if 200 <= response.status_code < 300:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise WorkerUnavailableError("OmniRoute worker returned invalid JSON") from exc
                if not isinstance(body, dict):
                    raise WorkerUnavailableError("OmniRoute worker returned a non-object response")
                return WorkerResult(
                    request.task_id,
                    request.model,
                    model.model_id,
                    attempts,
                    (time.perf_counter() - started) * 1000,
                    response=body,
                )
            if response.status_code in _RETRYABLE_STATUS_CODES or response.status_code >= 500:
                last_error = f"OmniRoute worker returned HTTP {response.status_code}"
                await self._backoff(attempts)
                continue
            raise WorkerUnavailableError(f"OmniRoute worker returned HTTP {response.status_code}")
        raise WorkerUnavailableError(last_error)

    async def _backoff(self, attempts: int) -> None:
        if self.retry_backoff_seconds:
            await asyncio.sleep(self.retry_backoff_seconds * attempts)

    @staticmethod
    def _select_models(
        request: WorkerRequest, models: tuple[WorkerModel, ...]
    ) -> list[WorkerModel]:
        by_id = {item.model_id: item for item in models}
        requested = request.model.strip()
        if requested and not requested.startswith("auto/") and requested in by_id:
            return [by_id[requested]]
        if requested and not requested.startswith("auto/"):
            return []

        key = requested.removeprefix("auto/").split(":", 1)[0] or "free"
        aliases = _AUTO_MODEL_ALIASES.get(key, (requested, "auto/best-free", "auto/fast"))
        selected: list[WorkerModel] = []
        seen: set[str] = set()
        for model_id in aliases:
            model = by_id.get(model_id)
            if (
                model
                and model.model_id not in seen
                and model.supports(request.required_capabilities)
            ):
                selected.append(model)
                seen.add(model.model_id)
        for model in models:
            if model.model_id in seen or not model.supports(request.required_capabilities):
                continue
            if model.model_id.endswith(":free") or model.model_id.startswith("auto/"):
                selected.append(model)
                seen.add(model.model_id)
        return selected


class WorkerPool:
    """Run independent worker tasks with an explicit concurrency ceiling."""

    def __init__(self, client: OmniRouteWorkerClient, *, max_concurrency: int = 4) -> None:
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 64:
            raise ValueError("max_concurrency must be an integer between 1 and 64")
        self.client = client
        self.max_concurrency = max_concurrency

    async def run(self, requests: Iterable[WorkerRequest]) -> list[WorkerResult]:
        pending = list(requests)
        semaphore = asyncio.Semaphore(self.max_concurrency)

        async def execute(item: WorkerRequest) -> WorkerResult:
            async with semaphore:
                started = time.perf_counter()
                try:
                    return await self.client.complete(item)
                except WorkerError as exc:
                    return WorkerResult(
                        item.task_id,
                        item.model,
                        None,
                        0,
                        (time.perf_counter() - started) * 1000,
                        error=str(exc),
                    )

        return list(await asyncio.gather(*(execute(item) for item in pending)))


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


__all__ = [
    "OmniRouteWorkerClient",
    "WorkerError",
    "WorkerModel",
    "WorkerPool",
    "WorkerRequest",
    "WorkerResult",
    "WorkerUnavailableError",
]

# Short compatibility alias for callers that prefer the noun form.
WorkerUnavailable = WorkerUnavailableError
