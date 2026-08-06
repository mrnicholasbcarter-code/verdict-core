from __future__ import annotations

import asyncio
import codecs
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, cast

try:
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel
    from starlette.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the web server mode. Install with `pip install verdict[server]`"
    ) from exc

from verdict.availability import OmniRouteAvailabilityAdapter
from verdict.availability_cache import AvailabilityCache
from verdict.catalog import configured_catalog_filters, normalize_catalog
from verdict.contracts import redact_contract_secrets
from verdict.eligibility import EligibilityGate
from verdict.evidence import (
    AmbiguousEvidenceSelectorError,
    DurableEvidenceStore,
    EvidenceStore,
    ExplainEvidence,
    build_outcome_event,
    build_routing_decision_contract,
    request_features,
)
from verdict.gate import Gate
from verdict.guidance import (
    GuidanceConfig,
    GuidanceConfigurationError,
    GuidanceControlPlane,
    GuidanceUnavailableError,
)
from verdict.intelligence import DEFAULT_PROFILE, DEFAULT_TIMEOUT_MS, IntelligenceService
from verdict.model_passports import ModelPassport
from verdict.models import ModelInfo, ProviderConfig
from verdict.omniroute import OmniRouteHTTPTransport
from verdict.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy
from verdict.relay import (
    build_attempts,
    failure_class,
    idempotency_key,
    is_opaque_alias,
    protocol_for_surface,
    response_actual_route_status,
    retry_safety,
    retryable_exception,
    retryable_response_status,
    transition_edge,
)
from verdict.security import bearer_matches, redact_text, validate_server_security


class _EvidenceStreamAdapter:
    """Own stream iteration, terminalization, and upstream cleanup."""

    def __init__(
        self,
        upstream: AsyncIterator[bytes],
        *,
        on_terminal: Any,
        event_factory: Any,
        event_prefix: str = "chat_completion",
    ) -> None:
        self._upstream = upstream
        self._on_terminal = on_terminal
        self._event_factory = event_factory
        self._event_prefix = event_prefix
        self._terminal = False
        self._closed = False
        self._cleanup_task: asyncio.Task[Any] | None = None
        self._cleanup_error: str | None = None

    def __aiter__(self) -> _EvidenceStreamAdapter:
        return self

    async def __anext__(self) -> bytes:
        try:
            return await self._upstream.__anext__()
        except StopAsyncIteration:
            cleanup_error = await self._cleanup()
            self._finish(f"{self._event_prefix}_streamed", "success", "completed", cleanup_error)
            raise
        except asyncio.CancelledError:
            cleanup_error = await self._cleanup()
            self._finish(
                f"{self._event_prefix}_stream_aborted", "cancelled", "aborted", cleanup_error
            )
            raise
        except _StreamProtocolError as exc:
            cleanup_error = await self._cleanup()
            self._finish(
                f"{self._event_prefix}_stream_error",
                "error",
                "error",
                cleanup_error,
                error_class=getattr(exc, "code", None) or type(exc).__name__,
            )
            raise StopAsyncIteration from None
        except Exception as exc:
            cleanup_error = await self._cleanup()
            self._finish(
                f"{self._event_prefix}_stream_error",
                "error",
                "error",
                cleanup_error,
                error_class=(getattr(exc, "code", None) or type(exc).__name__),
            )
            raise

    async def aclose(self) -> None:
        cleanup_error = await self._cleanup()
        self._finish(f"{self._event_prefix}_stream_aborted", "cancelled", "aborted", cleanup_error)

    async def _cleanup(self) -> str | None:
        if self._closed:
            if self._cleanup_task is not None:
                with suppress(BaseException):
                    await asyncio.shield(self._cleanup_task)
            return self._cleanup_error
        self._closed = True
        close = getattr(self._upstream, "aclose", None)
        if not callable(close):
            return None
        self._cleanup_task = asyncio.create_task(close())
        try:
            await asyncio.shield(self._cleanup_task)
        except BaseException as exc:
            self._cleanup_error = type(exc).__name__
            if not self._cleanup_task.done():
                self._cleanup_task.cancel()
            with suppress(BaseException):
                await asyncio.shield(self._cleanup_task)
        return self._cleanup_error

    def _finish(
        self,
        event_type: str,
        outcome: str,
        phase: str,
        cleanup_error: str | None,
        *,
        error_class: str | None = None,
    ) -> None:
        if self._terminal:
            return
        self._terminal = True
        self._on_terminal(
            self._event_factory(event_type, outcome, phase, cleanup_error, error_class)
        )


class _StreamProtocolError(RuntimeError):
    """Raised when an upstream stream is malformed or ends without a terminal event."""

    code = "malformed_stream"


class _ValidatedSSEStream:
    """Validate SSE framing incrementally while yielding the original bytes unchanged."""

    def __init__(
        self,
        upstream: AsyncIterator[bytes],
        *,
        surface: str,
        max_bytes: int = 16 * 1024 * 1024,
        max_buffer: int = 256 * 1024,
        max_events: int = 100_000,
    ) -> None:
        self._upstream = upstream
        self._surface = surface
        self._max_bytes = max_bytes
        self._max_buffer = max_buffer
        self._max_events = max_events
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._buffer = ""
        self._event_data: list[str] = []
        self._bytes = 0
        self._events = 0
        self._terminal = False
        self._closed = False

    def __aiter__(self) -> _ValidatedSSEStream:
        return self

    async def __anext__(self) -> bytes:
        try:
            chunk = await self._upstream.__anext__()
        except StopAsyncIteration:
            self._finish_text(self._decoder.decode(b"", final=True))
            if self._event_data:
                self._finish_event()
            if not self._terminal:
                raise _StreamProtocolError("stream ended without a terminal event") from None
            raise
        except UnicodeDecodeError as exc:
            raise _StreamProtocolError("stream contained invalid UTF-8") from exc
        if not isinstance(chunk, bytes):
            raise _StreamProtocolError("upstream stream yielded a non-byte chunk")
        self._bytes += len(chunk)
        if self._bytes > self._max_bytes:
            raise _StreamProtocolError("stream exceeded the aggregate byte limit")
        try:
            decoded = self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise _StreamProtocolError("stream contained invalid UTF-8") from exc
        self._finish_text(decoded)
        return chunk

    def _finish_text(self, text: str) -> None:
        self._buffer += text
        if len(self._buffer) > self._max_buffer:
            raise _StreamProtocolError("stream event buffer exceeded its limit")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if len(line) > self._max_buffer:
                raise _StreamProtocolError("stream line exceeded its limit")
            self._consume_line(line.rstrip("\r"))

    def _consume_line(self, line: str) -> None:
        if line == "":
            if self._event_data:
                self._finish_event()
            return
        if line.startswith(":"):
            return
        if line.startswith("data:"):
            self._event_data.append(line[5:].lstrip())
            return
        # ``event:``, ``id:``, and ``retry:`` are valid SSE metadata. Unknown
        # fields are ignored by the SSE standard and remain wire-transparent.

    def _finish_event(self) -> None:
        data = "\n".join(self._event_data)
        self._event_data.clear()
        self._events += 1
        if self._events > self._max_events:
            raise _StreamProtocolError("stream exceeded its event limit")
        if self._terminal:
            raise _StreamProtocolError("stream emitted data after its terminal event")
        if self._surface == "chat" and data == "[DONE]":
            self._terminal = True
            return
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _StreamProtocolError("stream event was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise _StreamProtocolError("stream event must be a JSON object")
        if self._surface == "responses":
            event_type = parsed.get("type")
            if event_type in {"response.completed", "response.failed", "response.incomplete"}:
                self._terminal = True
        else:
            choices = parsed.get("choices")
            if isinstance(choices, list) and any(
                isinstance(choice, dict) and choice.get("finish_reason") is not None
                for choice in choices
            ):
                self._terminal = True

    @property
    def terminal(self) -> bool:
        return self._terminal

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self._upstream, "aclose", None)
        if callable(close):
            await close()


async def _prime_stream(result: StreamedUpstreamResponse) -> StreamedUpstreamResponse:
    """Read the first upstream chunk before committing to a client stream."""

    body = result.body
    try:
        first = await body.__anext__()
    except StopAsyncIteration as exc:
        raise _StreamProtocolError("stream ended before its first byte") from exc

    async def primed_body() -> AsyncIterator[bytes]:
        yield first
        async for chunk in body:
            yield chunk

    return replace(result, body=primed_body())


class _EvidenceStreamingResponse(StreamingResponse):
    """Close the evidence-owned iterator even when ASGI send fails."""

    def __init__(self, content: _EvidenceStreamAdapter, **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._evidence_stream = content

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._evidence_stream.aclose()


class ModelPassportStore:
    """Bounded in-memory isolation-cache for qualified model passports.

    Keys are ``(provider, model_id)``; entries expire after a fixed TTL so a
    fresh qualification is never served stale, and the same key is never
    re-probed within the TTL window (isolation-key caching).
    """

    def __init__(self, *, ttl_seconds: int = 300) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], tuple[datetime, ModelPassport]] = {}

    def get(
        self, provider: str, model_id: str, *, now: datetime | None = None
    ) -> ModelPassport | None:
        current = now if now is not None else datetime.now(timezone.utc)
        entry = self._entries.get((provider, model_id))
        if entry is None:
            return None
        expires_at, passport = entry
        if current > expires_at:
            self._entries.pop((provider, model_id), None)
            return None
        return passport

    def put(self, passport: ModelPassport, *, now: datetime | None = None) -> None:
        current = now if now is not None else datetime.now(timezone.utc)
        self._entries[(passport.provider, passport.model_id)] = (
            current.replace(second=0, microsecond=0) + timedelta(seconds=self._ttl),
            passport,
        )

    def list_fresh(self, *, now: datetime | None = None) -> list[ModelPassport]:
        current = now if now is not None else datetime.now(timezone.utc)
        fresh: list[ModelPassport] = []
        for key, (expires_at, passport) in list(self._entries.items()):
            if current <= expires_at:
                fresh.append(passport)
            else:
                self._entries.pop(key, None)
        return fresh


# Singleton service instances
intelligence_instance: IntelligenceService | None = None
gate_instance: Gate | None = None
proxy_instance: UpstreamProxy | None = None
availability_cache_instance: AvailabilityCache | None = None
eligibility_gate_instance: EligibilityGate | None = None
evidence_store_instance: EvidenceStore | DurableEvidenceStore | None = None
guidance_plane_instance: GuidanceControlPlane | None = None
model_passport_store_instance: ModelPassportStore | None = None

DEFAULT_AVAILABILITY_TTL_SECONDS = 60
DEFAULT_AVAILABILITY_STALE_WINDOW_SECONDS = 30


def _build_availability_cache() -> tuple[AvailabilityCache, EligibilityGate] | None:
    """Build the bounded availability cache backed by the native OmniRoute transport.

    Returns ``None`` when no OmniRoute endpoint is configured, so the server
    still boots without availability explainability.  The transport is
    loopback-only and credential-safe; a misconfigured base URL fails closed
    to ``None`` rather than crashing startup.
    """
    base_url = os.getenv("OMNIROUTE_BASE_URL") or os.getenv("LLMGATE_UPSTREAM_BASE_URL")
    if not base_url or base_url.strip().lower() in {"", "none"}:
        return None
    api_key = os.getenv("OMNIROUTE_API_KEY")
    management_token = os.getenv("OMNIROUTE_MANAGEMENT_TOKEN")
    usage_api_key_id = os.getenv("OMNIROUTE_USAGE_API_KEY_ID")
    allow_private = _allowed_private_hosts() | {
        item.strip().lower()
        for item in os.getenv("OMNIROUTE_ALLOW_PRIVATE_HOSTS", "").split(",")
        if item.strip()
    }
    try:
        transport = OmniRouteHTTPTransport(
            base_url,
            api_key=api_key,
            management_token=management_token,
            usage_api_key_id=usage_api_key_id,
            allow_private_hosts=allow_private,
        )
    except Exception:
        return None
    adapter: OmniRouteAvailabilityAdapter = OmniRouteAvailabilityAdapter(transport)
    # Issue #57 root cause: enrich the adapter with bounded live probes when the
    # production availability profile is enabled.  Reuses ProbeRunner + the
    # documented openai_probe_transport; disabled by default (development).
    probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
    probe_enabled = os.getenv("LLMGATE_AVAILABILITY_PROFILE", "development").lower() == "production"
    if probe_enabled and probe_base_url:
        from verdict.availability import ProbeEnrichedAdapter
        from verdict.probes import openai_probe_transport

        probe_consented = os.getenv("LLMGATE_ALLOW_LIVE_PROBES", "").lower() in {"1", "true", "yes"}
        probe_transport = openai_probe_transport(
            probe_base_url, api_key=os.getenv("LLMGATE_PROBE_API_KEY") or api_key
        )
        enriched: Any = ProbeEnrichedAdapter(
            adapter,
            probe_transport=probe_transport,
            enabled=True,
            live=True,
            consented=probe_consented,
            provider="omniroute",
        )
    else:
        enriched = adapter
    cache = AvailabilityCache(
        source=enriched.evaluate,
        ttl_seconds=DEFAULT_AVAILABILITY_TTL_SECONDS,
        stale_window_seconds=DEFAULT_AVAILABILITY_STALE_WINDOW_SECONDS,
    )
    from verdict.eligibility import EligibilityGate

    gate = EligibilityGate(cache.get, protected_fail_closed=True, allow_unverified_in_dev=True)
    return cache, gate


DEFAULT_UPSTREAM_BASE_URL = "http://127.0.0.1:20132/v1"
DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024
DEFAULT_ALLOWED_PRIVATE_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _allowed_private_hosts() -> set[str]:
    configured = os.getenv("LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS", "")
    return DEFAULT_ALLOWED_PRIVATE_HOSTS | {
        item.strip().lower() for item in configured.split(",") if item.strip()
    }


def _build_proxy() -> UpstreamProxy:
    """Build the configured upstream transport without reading client fields."""
    base_url = os.getenv("LLMGATE_UPSTREAM_BASE_URL", DEFAULT_UPSTREAM_BASE_URL)
    api_key = os.getenv("LLMGATE_UPSTREAM_API_KEY") or os.getenv("OMNIROUTE_API_KEY")
    timeout_ms = int(os.getenv("LLMGATE_UPSTREAM_TIMEOUT_MS", "30000"))
    if timeout_ms <= 0:
        raise ValueError("LLMGATE_UPSTREAM_TIMEOUT_MS must be positive")
    return UpstreamProxy(
        base_url,
        api_key=api_key,
        timeout=timeout_ms / 1000,
        allow_private_hosts=_allowed_private_hosts(),
    )


def _build_intelligence() -> IntelligenceService:
    """Build the public IntelligenceService boundary from environment settings."""
    profile = os.getenv("LLMGATE_INTELLIGENCE_PROFILE", DEFAULT_PROFILE)
    timeout_ms = int(os.getenv("LLMGATE_INTELLIGENCE_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS)))
    allow_client_model_override = os.getenv(
        "LLMGATE_ALLOW_CLIENT_MODEL_OVERRIDE", "false"
    ).lower() in {"1", "true", "yes", "on"}
    frontier_allowlist_raw = os.getenv("LLMGATE_FRONTIER_ALLOWLIST")
    frontier_allowlist = (
        tuple(item.strip() for item in frontier_allowlist_raw.split(",") if item.strip())
        if frontier_allowlist_raw
        else None
    )
    providers: dict[str, ProviderConfig] = {}
    return IntelligenceService(
        primary_model=os.getenv("LLMGATE_PRIMARY", "anthropic/claude-3-opus-20240229"),
        providers=providers,
        profile=profile,
        log_path=os.getenv("LLMGATE_LOG_PATH", "verdict-decisions.jsonl"),
        log_full_task=False,
        discovery_ttl=int(os.getenv("LLMGATE_DISCOVERY_TTL_SECONDS", "60")),
        ruflo_command=os.getenv("LLMGATE_RUFLO_COMMAND", "ruflo"),
        ruvector_command=os.getenv("LLMGATE_RUVECTOR_COMMAND", "ruvector"),
        timeout_ms=timeout_ms,
        frontier_allowlist=frontier_allowlist,
        allow_client_model_override=allow_client_model_override,
    )


@asynccontextmanager
def _build_model_passport_store() -> ModelPassportStore:
    """Build the in-memory model-passport isolation cache.

    Qualification requires a probe endpoint.  When none is configured the store
    is still built (empty) so ``/v1/models/qualify`` fails closed with a clear
    503 instead of crashing startup — same posture as the availability cache.
    """
    probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
    if not probe_base_url or probe_base_url.strip().lower() in {"", "none"}:
        return ModelPassportStore()
    ttl = max(1, int(os.getenv("LLMGATE_MODEL_PASSPORT_TTL", "300")))
    return ModelPassportStore(ttl_seconds=ttl)


async def lifespan(app: FastAPI) -> Any:
    global intelligence_instance, gate_instance, proxy_instance, evidence_store_instance
    global guidance_plane_instance
    intelligence_instance = _build_intelligence()
    gate_instance = Gate(
        primary_model=intelligence_instance.primary_model,
        providers=intelligence_instance.providers,
        intelligence_service=intelligence_instance,
    )
    proxy_instance = _build_proxy()
    global availability_cache_instance, eligibility_gate_instance
    global model_passport_store_instance
    model_passport_store_instance = _build_model_passport_store()
    built = _build_availability_cache()
    availability_cache_instance, eligibility_gate_instance = (
        built if built is not None else (None, None)
    )
    evidence_db = os.getenv("VERDICT_RECEIPTS_DB") or os.getenv("VERDICT_EVIDENCE_DB")
    max_entries = max(1, int(os.getenv("VERDICT_EVIDENCE_MAX_ENTRIES", "256")))
    if evidence_db:
        evidence_store_instance = DurableEvidenceStore(evidence_db, max_entries=max_entries)
    elif os.getenv("PYTEST_CURRENT_TEST") or os.getenv(
        "LLMGATE_ALLOW_ANONYMOUS", "false"
    ).lower() in {"1", "true", "yes", "on"}:
        # Anonymous development/test mode has an explicit in-memory backend.
        # Authenticated deployments must configure a durable DB path.
        evidence_store_instance = DurableEvidenceStore(":memory:", max_entries=max_entries)
    else:
        raise RuntimeError("VERDICT_RECEIPTS_DB must be configured for authenticated API mode")
    # Guidance is opt-in and host-neutral. Keep normal routing startup
    # independent of project instruction files and optional agent tooling.
    try:
        guidance_config = GuidanceConfig.from_environment()
    except GuidanceConfigurationError as exc:
        # Invalid opt-in configuration must be visible as degraded guidance,
        # while leaving the normal routing service available.
        guidance_config = GuidanceConfig(
            enabled=True, repo_root=Path.cwd(), guidance_path=Path.cwd() / "GUIDANCE.md"
        )
        guidance_plane_instance = GuidanceControlPlane.degraded(guidance_config, str(exc))
    else:
        if not guidance_config.enabled:
            guidance_plane_instance = GuidanceControlPlane.disabled(guidance_config)
        else:
            guidance_plane_instance = await GuidanceControlPlane.initialize(guidance_config)
    # Issue #57: feed the eligibility gate into the IntelligenceService so the
    # live routing path filters before ranking (single source of truth).
    if eligibility_gate_instance is not None:
        intelligence_instance.eligibility_gate = eligibility_gate_instance
    yield
    intelligence_instance = None
    gate_instance = None
    proxy_instance = None
    availability_cache_instance = None
    eligibility_gate_instance = None
    evidence_store_instance = None
    guidance_plane_instance = None
    model_passport_store_instance = None


app = FastAPI(
    title="verdict API",
    description="Microservice for Tier-based LLM Routing",
    version="0.2.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def caller_authentication(request: Request, call_next: Any) -> Response:
    """Require server-owned bearer auth for every non-health API route."""
    if request.url.path == "/health":
        return cast(Response, await call_next(request))
    token = os.getenv("LLMGATE_AUTH_TOKEN")
    anonymous = os.getenv("LLMGATE_ALLOW_ANONYMOUS", "false").lower() in {"1", "true", "yes", "on"}
    if anonymous and not token:
        return cast(Response, await call_next(request))
    if not token:
        return _proxy_error(503, "server authentication is not configured")
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return JSONResponse(
            status_code=401,
            content={
                "error": {"message": "authentication required", "type": "authentication_error"}
            },
            headers={"www-authenticate": "Bearer"},
        )
    if not bearer_matches(supplied, token):
        return _proxy_error(403, "authentication failed")
    return cast(Response, await call_next(request))


class RouteRequest(BaseModel):
    task: str
    criticality: str = "medium"
    model: str | None = None
    allow_client_model_override: bool = False
    protected: bool = False
    privacy_class: str = "any"
    tools_required: bool = False
    structured_output_required: bool = False
    vision_required: bool = False
    streaming_required: bool = False
    request_id: str | None = None
    correlation_id: str | None = None


async def _route_with_intelligence(
    task: str, criticality: str, context: dict[str, Any] | None = None
) -> Any:
    if intelligence_instance is None:
        raise HTTPException(status_code=503, detail="Intelligence service not initialized")
    return await intelligence_instance.route(task, criticality=criticality, context=context)


@app.post("/v1/route")
async def route_task(request: Request, req: RouteRequest) -> Response:
    context = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    decision = await _route_with_intelligence(req.task, req.criticality, context=context)
    route_evidence, evidence_key = _start_evidence(
        decision,
        task=req.task,
        criticality=req.criticality,
        features={
            "stream": req.streaming_required,
            "tools": req.tools_required,
            "response_format": "structured" if req.structured_output_required else None,
            "vision": req.vision_required,
            "tool_count": 0,
            "tool_names": [],
        },
        request_id=req.request_id,
        correlation_id=req.correlation_id,
        scope=_evidence_scope(request),
    )
    outcome = build_outcome_event(
        route_evidence.routing_decision,
        event_type="route_decision_recorded",
        outcome="denied" if decision.decision == "denied" else "success",
        features={"route_only": True},
    )
    route_evidence = _finish_evidence(route_evidence, outcome, evidence_key)
    status_code = 200 if decision.decision != "denied" else 503
    headers: dict[str, str] = {}
    if route_evidence.evidence_id:
        headers["x-verdict-evidence-id"] = route_evidence.evidence_id
    headers["x-verdict-evidence-request-id"] = route_evidence.routing_decision.request_id or ""
    headers["x-verdict-correlation-id"] = route_evidence.routing_decision.correlation_id or ""
    return JSONResponse(
        content=_safe_decision_dict(decision), status_code=status_code, headers=headers
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "engine": "verdict"}


@app.get("/v1/guidance/status")
async def guidance_status() -> dict[str, Any]:
    """Return opt-in guidance status without exposing policy contents."""

    if guidance_plane_instance is None:
        return {"status": "disabled", "enabled": False, "engine": "verdict"}
    return {
        "status": guidance_plane_instance.status.state,
        "enabled": guidance_plane_instance.status.enabled,
        "reason": guidance_plane_instance.status.reason,
        "policy_version": guidance_plane_instance.status.policy_version,
        "initialization_ms": guidance_plane_instance.status.initialization_ms,
        "engine": "verdict",
    }


@app.get("/ready")
async def ready() -> Response:
    """Report process readiness and verify that the configured upstream responds."""
    if intelligence_instance is None or proxy_instance is None:
        raise HTTPException(status_code=503, detail="Gate engine not initialized")

    intel = intelligence_instance.readiness()
    try:
        upstream = await proxy_instance.models()
        upstream_ok = upstream.status_code < 400
    except Exception as exc:
        upstream_ok = False
        upstream = None
        upstream_error = str(exc)
    else:
        upstream_error = ""

    overall_status = intel.status if upstream_ok else "not_ready"
    status_code = 200 if overall_status in {"ready", "degraded"} else 503
    content: dict[str, Any] = {
        "status": overall_status,
        "engine": "verdict",
        "intelligence": asdict(intel),
        "upstream": "[configured]",
    }
    if upstream is not None:
        content["upstream_status_code"] = upstream.status_code
    if upstream_error:
        content["reason"] = redact_text(upstream_error)
    elif intel.reason:
        content["reason"] = intel.reason
    return JSONResponse(status_code=status_code, content=content)


class GuidanceTaskRequest(BaseModel):
    schema_version: str
    task: dict[str, Any]


@app.post("/v1/guidance/execute")
async def execute_guidance(request: Request, payload: GuidanceTaskRequest) -> Response:
    """Evaluate one versioned, platform-neutral guidance request."""

    del request
    if guidance_plane_instance is None or not guidance_plane_instance.status.enabled:
        return _proxy_error(404, "guidance is disabled")
    if payload.schema_version != "1":
        return _proxy_error(400, "unsupported guidance schema_version")
    if guidance_plane_instance.status.state != "ready":
        return _proxy_error(
            503,
            "guidance is degraded",
            extra={"guidance_status": guidance_plane_instance.status.reason},
        )
    goal = payload.task.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return _proxy_error(422, "task.goal must be a non-empty string")
    try:
        result = guidance_plane_instance.evaluate(payload.task)
    except GuidanceUnavailableError as exc:
        return _proxy_error(503, str(exc))
    return JSONResponse(content=result)


def _proxy_error(
    status_code: int,
    message: str,
    *,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"message": message, "type": "invalid_request_error"}}
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


def _safe_decision_dict(decision: Any) -> dict[str, Any]:
    """Serialize legacy compatibility data without exposing diagnostic secrets."""

    return cast(dict[str, Any], redact_contract_secrets(asdict(decision)))


def _task_text(payload: dict[str, Any]) -> str:
    messages = payload.get("messages", payload.get("input", []))
    if isinstance(messages, str):
        return messages
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if isinstance(message, str):
            parts.append(message)
            continue
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.extend(
                    item["text"]
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            elif isinstance(message.get("text"), str):
                parts.append(message["text"])
    return "\n".join(parts)


def _as_response(result: BufferedUpstreamResponse) -> Response:
    return Response(
        content=result.body, status_code=result.status_code, headers=dict(result.headers)
    )


def _headers_for_body(result: BufferedUpstreamResponse) -> dict[str, str]:
    headers = dict(result.headers)
    headers.pop("content-length", None)
    return headers


def _relay_response_headers(
    result: BufferedUpstreamResponse | StreamedUpstreamResponse,
) -> dict[str, str]:
    """Return safe upstream metadata without stale framing or forged Verdict headers."""

    headers = dict(result.headers)
    headers.pop("content-length", None)
    headers.pop("content-encoding", None)
    return {
        name: value for name, value in headers.items() if not name.lower().startswith("x-verdict-")
    }


def _evidence_scope(request: Request) -> str:
    """Bind evidence lookup to the authenticated deployment principal."""

    if os.getenv("LLMGATE_AUTH_TOKEN"):
        return "server-auth"
    # Anonymous mode intentionally has one explicit, non-authoritative scope;
    # a caller-controlled header must never create an authorization boundary.
    return "anonymous"


def _evidence_headers(evidence: ExplainEvidence) -> dict[str, str]:
    """Expose correlation metadata without putting evidence on legacy bodies."""

    headers = {
        "x-verdict-evidence-request-id": evidence.routing_decision.request_id or "",
        "x-verdict-correlation-id": evidence.routing_decision.correlation_id or "",
    }
    if evidence.evidence_id:
        headers["x-verdict-evidence-id"] = evidence.evidence_id
    return headers


@app.get("/v1/route/explain")
async def route_explain(
    request: Request,
    model_id: str | None = None,
    evidence_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> Response:
    """Explain availability freshness and eligibility for one or all cached models.

    Implements the issue #56 / #73 explain contract: surfaces observed_at,
    expires_at, age, source, confidence, candidate/eligible counts, per-candidate
    exclusion reasons (#73), and cache refresh/error state (``cache_state``,
    ``stale``, ``refreshing``, ``refresh_error``).

    Without ``model_id`` the response reports the cache scope (policy version and
    configured model keys) plus the gate's pre-ranking eligible/exclusion sets
    when available. With ``model_id`` it returns the per-model freshness explain
    record, refreshing on first access.
    """
    # Evidence lookup is independent of live availability. This lets an
    # operator inspect the immutable decision-time record even after a cache
    # expires or OmniRoute is temporarily unavailable.
    if sum(value is not None for value in (model_id, evidence_id, request_id, correlation_id)) > 1:
        return _proxy_error(400, "provide exactly one explain query selector")
    if evidence_id or request_id or correlation_id:
        try:
            evidence = (
                evidence_store_instance.find(
                    evidence_id=evidence_id,
                    request_id=request_id,
                    correlation_id=correlation_id,
                    scope=_evidence_scope(request),
                )
                if evidence_store_instance is not None
                else None
            )
        except AmbiguousEvidenceSelectorError:
            return _proxy_error(409, "evidence selector is ambiguous; use evidence_id")
        if evidence_store_instance is None:
            return _proxy_error(503, "execution evidence is unavailable")
        if evidence is None:
            return _proxy_error(404, "routing evidence not found")
        return JSONResponse(content=evidence.to_dict())

    if availability_cache_instance is None:
        return _proxy_error(
            503, "availability cache not configured (set OMNIROUTE_BASE_URL to enable)"
        )
    if model_id is None or model_id == "":
        base: dict[str, Any] = {
            "kind": "availability_explain",
            "policy_version": availability_cache_instance.policy_version,
            "cached_models": sorted(availability_cache_instance.keys()),
            "cache_state": "configured",
        }
        # Issue #73: surface the gate's complete pre-ranking eligible set and
        # exclusions from the same authority the router uses.
        if eligibility_gate_instance is not None:
            gate_eval = eligibility_gate_instance.evaluate(
                [
                    ModelInfo(
                        id=mid,
                        provider=mid.split("/", 1)[0] if "/" in mid else "unknown",
                        capability_tier=2,
                    )
                    for mid in base["cached_models"]
                ],
                dev_mode=True,
            )
            base["eligible_set"] = [m.id for m in gate_eval.eligible]
            base["exclusions"] = [r.to_dict() for r in gate_eval.exclusions]
        return JSONResponse(content=base)
    if model_id is None:
        return _proxy_error(400, "model_id must not be null")
    record = availability_cache_instance.explain(model_id)
    record["kind"] = "availability_explain"
    if eligibility_gate_instance is not None:
        gate_eval = eligibility_gate_instance.evaluate(
            [
                ModelInfo(
                    id=model_id,
                    provider=model_id.split("/", 1)[0] if "/" in model_id else "unknown",
                    capability_tier=2,
                )
            ],
            dev_mode=True,
        )
        if gate_eval.records:
            record["eligibility"] = gate_eval.records[0].to_dict()
            record["eligible"] = gate_eval.records[0].admitted
    return JSONResponse(content=record)


@app.get("/v1/models")
async def list_models() -> Response:
    """Return a locally filtered catalog with conservative availability metadata."""
    if proxy_instance is None:
        raise HTTPException(status_code=503, detail="Proxy not initialized")
    try:
        result = await proxy_instance.models()
    except Exception:
        return _proxy_error(502, "upstream model catalog unavailable")
    allowlist, denylist = configured_catalog_filters(
        os.getenv("LLMGATE_MODEL_ALLOWLIST"), os.getenv("LLMGATE_MODEL_DENYLIST")
    )
    filtered_body = normalize_catalog(result.body, allowlist=allowlist, denylist=denylist)
    return Response(
        content=filtered_body, status_code=result.status_code, headers=_headers_for_body(result)
    )


class QualifyRequest(BaseModel):
    """Body for ``POST /v1/models/qualify``."""

    provider: str
    model_id: str
    estimated_tokens: int | None = None
    require_tools: bool = False
    require_structured_output: bool = False


@app.post("/v1/models/qualify")
async def qualify_model(request: Request, req: QualifyRequest) -> Response:
    """Qualify one concrete provider model with a bounded probe cascade.

    Returns a fresh ``ModelPassport`` (isolation-key cached for the store TTL),
    or a fail-closed error when qualification is not configured.
    """
    if not req.provider.strip() or not req.model_id.strip():
        return _proxy_error(422, "provider and model_id must be non-empty")
    if "/" in req.provider or "/" in req.model_id:
        return _proxy_error(422, "provider and model_id must not contain '/'")
    if req.estimated_tokens is not None and req.estimated_tokens < 0:
        return _proxy_error(422, "estimated_tokens must be non-negative")
    if model_passport_store_instance is None:
        return _proxy_error(503, "model qualification is not configured")
    fresh = model_passport_store_instance.get(req.provider, req.model_id)
    if fresh is not None:
        return JSONResponse(content=fresh.to_dict())
    probe_base_url = os.getenv("LLMGATE_PROBE_BASE_URL")
    if not probe_base_url or probe_base_url.strip().lower() in {"", "none"}:
        return _proxy_error(503, "model qualification requires LLMGATE_PROBE_BASE_URL")
    try:
        from verdict.model_passports import run_qualification
        from verdict.probes import openai_probe_transport

        api_key = os.getenv("LLMGATE_PROBE_API_KEY") or os.getenv("LLMGATE_UPSTREAM_API_KEY")
        transport = openai_probe_transport(probe_base_url, api_key=api_key)
        live = os.getenv("LLMGATE_ALLOW_LIVE_PROBES", "").lower() in {"1", "true", "yes"}
        consented = os.getenv("LLMGATE_PROBE_CONSENTED", "").lower() in {"1", "true", "yes"}
        passport = await asyncio.to_thread(
            run_qualification,
            provider=req.provider,
            model_id=req.model_id,
            transport=transport,
            live=live,
            consented=consented,
            require_tools=req.require_tools,
            require_structured_output=req.require_structured_output,
            estimated_tokens=req.estimated_tokens,
        )
    except Exception:
        return _proxy_error(502, "model qualification failed")
    model_passport_store_instance.put(passport)
    return JSONResponse(content=passport.to_dict())


@app.get("/v1/models/passports")
async def list_passports(request: Request, model_ids: str | None = None) -> Response:
    """List fresh qualified model passports, optionally filtered by ids."""
    if model_passport_store_instance is None:
        return _proxy_error(503, "model qualification is not configured")
    if model_ids:
        wanted = [item.strip() for item in model_ids.split(",") if item.strip()]
        if not wanted:
            return _proxy_error(422, "model_ids must contain at least one id")
        found = []
        for item in wanted:
            if "/" not in item:
                return _proxy_error(422, f"model_id '{item}' must be provider/model")
            provider, model_id = item.split("/", 1)
            passport = model_passport_store_instance.get(provider, model_id)
            if passport is not None:
                found.append(passport.to_dict())
        return JSONResponse(content={"passports": found})
    passports = [p.to_dict() for p in model_passport_store_instance.list_fresh()]
    return JSONResponse(content={"passports": passports})


async def _relay_completion(request: Request, *, surface: str) -> Response:
    """Route and transparently forward one OpenAI protocol surface."""
    if intelligence_instance is None or proxy_instance is None:
        raise HTTPException(status_code=503, detail="Proxy not initialized")

    max_bytes = int(os.getenv("LLMGATE_MAX_REQUEST_BYTES", str(DEFAULT_MAX_REQUEST_BYTES)))
    body = await request.body()
    if len(body) > max_bytes:
        return _proxy_error(413, "request body exceeds configured size limit")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _proxy_error(400, "request body must be valid JSON")
    if not isinstance(payload, dict):
        return _proxy_error(400, "request body must be a JSON object")

    task = _task_text(payload)
    protocol = protocol_for_surface(surface)
    event_prefix = "chat_completion" if surface == "chat" else "responses"
    features = request_features(payload)
    correlation_id = request.headers.get("x-verdict-correlation-id")
    if not correlation_id and isinstance(payload.get("correlation_id"), str):
        correlation_id = cast(str, payload["correlation_id"])
    decision = await intelligence_instance.route(
        task, criticality=payload.get("criticality", "medium"), context=payload
    )
    criticality = payload.get("criticality", "medium")
    if not isinstance(criticality, str):
        criticality = "unknown"
    request_identity = (
        payload.get("model") if isinstance(payload.get("model"), str) else "unspecified"
    )
    request_key = idempotency_key(request, payload)
    safety = retry_safety(request, payload, request_key)
    attempts = build_attempts(proxy_instance, decision, protocol=protocol)
    if not attempts:
        return _proxy_error(503, "no executable route was selected")
    attempted_routes = [item.route.to_dict() for item in attempts]
    route_evidence, evidence_key = _start_evidence(
        decision,
        task=task,
        criticality=criticality,
        features=features,
        request_id=request.headers.get("x-verdict-request-id")
        or (payload.get("request_id") if isinstance(payload.get("request_id"), str) else None),
        correlation_id=correlation_id,
        scope=_evidence_scope(request),
        requested_identity=request_identity,
        resolved_route=attempts[0].route.to_dict(),
        attempted_routes=attempted_routes,
    )
    decision = replace(
        decision, request_id=route_evidence.routing_decision.request_id or decision.request_id
    )
    if decision.decision == "denied":
        outcome = build_outcome_event(
            route_evidence.routing_decision,
            event_type=f"{event_prefix}_denied",
            outcome="denied",
            features=features,
        )
        evidence = _finish_evidence(route_evidence, outcome, evidence_key)
        return _proxy_error(
            503,
            decision.reason,
            extra={"decision": _safe_decision_dict(decision)},
            headers=_evidence_headers(evidence),
        )

    if decision.protected and is_opaque_alias(attempts[0].model):
        evidence = _finish_evidence(
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_denied",
                outcome="denied",
                features=features,
                details={"failure_class": "opaque_route_unattested"},
            ),
            evidence_key,
        )
        return _proxy_error(
            503,
            "protected execution requires an attested actual route",
            headers=_evidence_headers(evidence),
        )
    started_at = monotonic()
    result: BufferedUpstreamResponse | StreamedUpstreamResponse | None = None
    attempts_used: list[dict[str, Any]] = []
    last_error: BaseException | None = None
    last_status: int | None = None

    def record_attempt_event(*, attempt: Any, event_type: str, details: dict[str, Any]) -> None:
        nonlocal route_evidence
        route_evidence = _finish_evidence(
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=event_type,
                outcome="unknown",
                features=features,
                details={
                    "attempt": len(attempts_used) - 1,
                    "model": attempt.model,
                    "route": attempt.route.to_dict(),
                    "policy_version": decision.policy_version,
                    **details,
                },
            ),
            evidence_key,
        )

    for index, attempt in enumerate(attempts):
        if index:
            edge = transition_edge(
                attempts,
                index - 1,
                request_id=route_evidence.routing_decision.request_id or decision.request_id,
                key=request_key,
                safety=safety,
                protocol=protocol,
                protected=decision.protected,
            )
            if edge is None or not edge.legal:
                break
        else:
            edge = None
        forwarded = dict(payload)
        # Verdict-local controls must never be forwarded to an upstream provider.
        for local_field in ("request_id", "correlation_id", "criticality", "idempotency_key"):
            forwarded.pop(local_field, None)
        forwarded["model"] = attempt.model
        try:
            if surface == "responses":
                result = await proxy_instance.responses(forwarded, idempotency_key=request_key)
            else:
                result = await proxy_instance.chat(forwarded, idempotency_key=request_key)
            if isinstance(result, StreamedUpstreamResponse):
                result = replace(result, body=_ValidatedSSEStream(result.body, surface=surface))
                result = await _prime_stream(result)
            last_status = result.status_code
            attempts_used.append(
                {
                    "model": attempt.model,
                    "route_key": attempt.route.key,
                    "outcome": "success" if result.status_code < 400 else "error",
                    "failure_class": None
                    if result.status_code < 400
                    else failure_class(result.status_code),
                    "transition_legal": True if index == 0 else bool(edge and edge.legal),
                    "compatibility_rule_version": result.compatibility_rule_version,
                }
            )
            record_attempt_event(
                attempt=attempt,
                event_type=(
                    f"{event_prefix}_attempt_completed"
                    if result.status_code < 400
                    else f"{event_prefix}_attempt_failed"
                ),
                details={
                    "status_code": result.status_code,
                    "failure_class": None
                    if result.status_code < 400
                    else failure_class(result.status_code),
                    "transition_edge": edge.to_dict() if edge is not None else None,
                    "compatibility_rule_version": result.compatibility_rule_version,
                },
            )
            if result.status_code < 400 or not retryable_response_status(
                result.status_code,
                compatibility_applied=result.compatibility_rule_version is not None,
            ):
                break
            last_error = None
        except asyncio.CancelledError:
            outcome = build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_cancelled",
                outcome="cancelled",
                features=features,
                abort_observed=True,
                latency_ms=(monotonic() - started_at) * 1000,
                details={"attempted_routes": attempts_used},
            )
            _finish_evidence(route_evidence, outcome, evidence_key)
            raise
        except Exception as exc:
            result = None
            last_error = exc
            attempts_used.append(
                {
                    "model": attempt.model,
                    "route_key": attempt.route.key,
                    "outcome": "error",
                    "failure_class": failure_class(error=exc),
                    "transition_legal": True if index == 0 else bool(edge and edge.legal),
                }
            )
            record_attempt_event(
                attempt=attempt,
                event_type=f"{event_prefix}_attempt_failed",
                details={
                    "failure_class": failure_class(error=exc),
                    "transition_edge": edge.to_dict() if edge is not None else None,
                },
            )
            if not retryable_exception(exc):
                break

    if result is None:
        evidence = _finish_evidence(
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_error",
                outcome="error",
                features=features,
                error_class=failure_class(last_status, last_error),
                retries=max(0, len(attempts_used) - 1),
                fallbacks=attempts_used[1:],
                latency_ms=(monotonic() - started_at) * 1000,
                details={"attempted_routes": attempts_used},
            ),
            evidence_key,
        )
        return _proxy_error(
            502,
            "upstream request failed",
            extra={
                "decision": _safe_decision_dict(
                    replace(decision, transport_outcome="upstream_error")
                )
            },
            headers=_evidence_headers(evidence),
        )

    transport_outcome = "success" if result.status_code < 400 else "upstream_error"
    decision_record = replace(decision, transport_outcome=transport_outcome)
    response_outcome = "success" if result.status_code < 400 else "error"
    response_headers = _relay_response_headers(result)
    response_headers["x-verdict-model"] = attempts_used[-1]["model"]
    response_headers["x-verdict-tier"] = str(decision_record.tier)
    response_headers["x-verdict-request-id"] = decision_record.request_id
    response_headers["x-verdict-decision"] = decision_record.decision
    response_headers["x-verdict-transport-outcome"] = decision_record.transport_outcome
    response_headers["x-verdict-quality-outcome"] = decision_record.quality_outcome
    response_headers["x-verdict-degraded-mode"] = str(decision_record.degraded_mode).lower()
    response_headers.update(_evidence_headers(route_evidence))

    if isinstance(result, BufferedUpstreamResponse):
        evidence = _finish_evidence(
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type=f"{event_prefix}_buffered",
                outcome=response_outcome,
                status_code=result.status_code,
                features=features,
                latency_ms=(monotonic() - started_at) * 1000,
                retries=max(0, len(attempts_used) - 1),
                fallbacks=attempts_used[1:],
                details={
                    "attempted_routes": attempts_used,
                    "compatibility_rule_version": result.compatibility_rule_version,
                },
            ),
            evidence_key,
        )
        if result.status_code >= 400:
            return Response(
                content=result.body, status_code=result.status_code, headers=response_headers
            )
        return Response(
            content=result.body, status_code=result.status_code, headers=response_headers
        )
    if isinstance(result, StreamedUpstreamResponse):

        def finalize_stream(event: Any) -> None:
            _finish_evidence(route_evidence, event, evidence_key)

        def stream_event(
            event_type: str,
            outcome: str,
            phase: str,
            cleanup_error: str | None,
            error_class: str | None,
        ) -> Any:
            details: dict[str, Any] = {
                "cleanup_status": "error" if cleanup_error else "closed",
                "cleanup_attempted": True,
            }
            if cleanup_error:
                details["cleanup_error_class"] = cleanup_error
            return build_outcome_event(
                route_evidence.routing_decision,
                event_type=event_type,
                outcome=outcome,
                status_code=result.status_code,
                features=features,
                streaming_phase=phase,
                abort_observed=phase != "completed",
                error_class=error_class,
                latency_ms=(monotonic() - started_at) * 1000,
                retries=max(0, len(attempts_used) - 1),
                fallbacks=attempts_used[1:],
                details={
                    **details,
                    "attempted_routes": attempts_used,
                    "compatibility_rule_version": result.compatibility_rule_version,
                    "actual_route": result.actual_route.to_dict()
                    if result.actual_route is not None
                    else None,
                    "actual_route_status": (response_actual_route_status(result)),
                },
            )

        evidence_stream = _EvidenceStreamAdapter(
            result.body,
            on_terminal=finalize_stream,
            event_factory=stream_event,
            event_prefix=event_prefix,
        )

        return _EvidenceStreamingResponse(
            evidence_stream,
            status_code=result.status_code,
            headers=response_headers,
            media_type=None,
        )
    raise TypeError(f"unsupported upstream response: {type(result)!r}")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Route and transparently forward an OpenAI chat completion request."""
    return await _relay_completion(request, surface="chat")


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    """Route and transparently forward an OpenAI Responses request."""
    return await _relay_completion(request, surface="responses")


def _start_evidence(
    decision: Any,
    *,
    task: str,
    criticality: str,
    features: dict[str, Any],
    request_id: str | None = None,
    correlation_id: str | None = None,
    scope: str,
    requested_identity: str | None = None,
    resolved_route: dict[str, Any] | None = None,
    attempted_routes: list[dict[str, Any]] | None = None,
) -> tuple[ExplainEvidence, str | None]:
    """Create and retain immutable decision evidence before upstream I/O."""

    routing = build_routing_decision_contract(
        decision,
        task=task,
        criticality=criticality,
        features=features,
        request_id=request_id,
        correlation_id=correlation_id,
        requested_identity=requested_identity,
        resolved_route=resolved_route,
        attempted_routes=attempted_routes,
    )
    started = build_outcome_event(
        routing,
        event_type="execution_started",
        outcome="unknown",
        features=features,
        streaming_phase="started" if features.get("stream") else None,
    )
    evidence = ExplainEvidence(routing, started)
    evidence_key = None
    if evidence_store_instance is not None:
        evidence_key = evidence_store_instance.put(evidence, scope=scope)
        stored = evidence_store_instance.find(evidence_id=evidence_key, scope=scope)
        if stored is not None:
            evidence = stored
    return evidence, evidence_key


def _finish_evidence(
    evidence: ExplainEvidence, event: Any, evidence_key: str | None = None
) -> ExplainEvidence:
    """Append a lifecycle event while retaining the decision-time snapshot."""

    events = evidence.events or (evidence.outcome_event,)
    updated = ExplainEvidence(evidence.routing_decision, event, events=(*events, event))
    if evidence_store_instance is not None and evidence_key is not None:
        stored = evidence_store_instance.update_outcome(evidence_key, event, scope=evidence.scope)
        if stored is not None:
            return stored
    return updated


@app.post("/route")
async def route_task_alias(request: Request, req: RouteRequest) -> Response:
    """Convenience alias matching the integration test client path."""
    response = await route_task(request, req)
    return response


def start_server(port: int = 8000, host: str | None = None) -> None:
    """Boot the uvicorn server with explicit production security defaults."""
    import uvicorn

    configured_host: str = (
        host if host is not None else cast(str, os.getenv("LLMGATE_HOST", "127.0.0.1"))
    )
    unix_socket = os.getenv("LLMGATE_UNIX_SOCKET") or None
    allow_anonymous = os.getenv("LLMGATE_ALLOW_ANONYMOUS", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    validate_server_security(
        host=configured_host,
        token=os.getenv("LLMGATE_AUTH_TOKEN") or None,
        allow_anonymous=allow_anonymous,
        unix_socket=unix_socket,
    )
    kwargs: dict[str, Any] = {"port": port}
    if unix_socket:
        kwargs["uds"] = unix_socket
    else:
        kwargs["host"] = configured_host
    uvicorn.run(app, **kwargs)
