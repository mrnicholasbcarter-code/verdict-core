from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, replace
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
    EvidenceStore,
    ExplainEvidence,
    build_outcome_event,
    build_routing_decision_contract,
    request_features,
)
from verdict.gate import Gate
from verdict.intelligence import DEFAULT_PROFILE, DEFAULT_TIMEOUT_MS, IntelligenceService
from verdict.models import ModelInfo, ProviderConfig
from verdict.omniroute import OmniRouteHTTPTransport
from verdict.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy
from verdict.security import bearer_matches, redact_text, validate_server_security


class _EvidenceStreamAdapter:
    """Own stream iteration, terminalization, and upstream cleanup."""

    def __init__(
        self, upstream: AsyncIterator[bytes], *, on_terminal: Any, event_factory: Any
    ) -> None:
        self._upstream = upstream
        self._on_terminal = on_terminal
        self._event_factory = event_factory
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
            self._finish("chat_completion_streamed", "success", "completed", cleanup_error)
            raise
        except asyncio.CancelledError:
            cleanup_error = await self._cleanup()
            self._finish("chat_completion_stream_aborted", "cancelled", "aborted", cleanup_error)
            raise
        except Exception as exc:
            cleanup_error = await self._cleanup()
            self._finish(
                "chat_completion_stream_error",
                "error",
                "error",
                cleanup_error,
                error_class=type(exc).__name__,
            )
            raise

    async def aclose(self) -> None:
        cleanup_error = await self._cleanup()
        self._finish("chat_completion_stream_aborted", "cancelled", "aborted", cleanup_error)

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
            self._event_factory(
                event_type,
                outcome,
                phase,
                cleanup_error,
                error_class,
            )
        )


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


# Singleton service instances
intelligence_instance: IntelligenceService | None = None
gate_instance: Gate | None = None
proxy_instance: UpstreamProxy | None = None
availability_cache_instance: AvailabilityCache | None = None
eligibility_gate_instance: EligibilityGate | None = None
evidence_store_instance: EvidenceStore | None = None

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

        probe_transport = openai_probe_transport(
            probe_base_url,
            api_key=os.getenv("LLMGATE_PROBE_API_KEY") or api_key,
        )
        enriched: Any = ProbeEnrichedAdapter(adapter, probe_transport=probe_transport, enabled=True)
    else:
        enriched = adapter
    cache = AvailabilityCache(
        source=enriched.evaluate,
        ttl_seconds=DEFAULT_AVAILABILITY_TTL_SECONDS,
        stale_window_seconds=DEFAULT_AVAILABILITY_STALE_WINDOW_SECONDS,
    )
    from verdict.eligibility import EligibilityGate

    gate = EligibilityGate(
        cache.get,
        protected_fail_closed=True,
        allow_unverified_in_dev=True,
    )
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
    ).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
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
async def lifespan(app: FastAPI) -> Any:
    global intelligence_instance, gate_instance, proxy_instance, evidence_store_instance
    intelligence_instance = _build_intelligence()
    gate_instance = Gate(
        primary_model=intelligence_instance.primary_model,
        providers=intelligence_instance.providers,
        intelligence_service=intelligence_instance,
    )
    proxy_instance = _build_proxy()
    global availability_cache_instance, eligibility_gate_instance
    built = _build_availability_cache()
    availability_cache_instance, eligibility_gate_instance = (
        built if built is not None else (None, None)
    )
    evidence_store_instance = EvidenceStore(
        max_entries=max(1, int(os.getenv("VERDICT_EVIDENCE_MAX_ENTRIES", "256")))
    )
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
    task: str,
    criticality: str,
    context: dict[str, Any] | None = None,
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
    messages = payload.get("messages", [])
    if not isinstance(messages, list):
        return ""
    parts: list[str] = []
    for message in messages:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                parts.append(content)
    return "\n".join(parts)


def _as_response(result: BufferedUpstreamResponse) -> Response:
    return Response(
        content=result.body, status_code=result.status_code, headers=dict(result.headers)
    )


def _headers_for_body(result: BufferedUpstreamResponse) -> dict[str, str]:
    headers = dict(result.headers)
    headers.pop("content-length", None)
    return headers


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
            503,
            "availability cache not configured (set OMNIROUTE_BASE_URL to enable)",
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
        content=filtered_body,
        status_code=result.status_code,
        headers=_headers_for_body(result),
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Route and transparently forward an OpenAI chat completion request."""
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
    route_evidence, evidence_key = _start_evidence(
        decision,
        task=task,
        criticality=criticality,
        features=features,
        request_id=request.headers.get("x-verdict-request-id")
        or (payload.get("request_id") if isinstance(payload.get("request_id"), str) else None),
        correlation_id=correlation_id,
        scope=_evidence_scope(request),
    )
    decision = replace(
        decision,
        request_id=route_evidence.routing_decision.request_id or decision.request_id,
    )
    if decision.decision == "denied":
        outcome = build_outcome_event(
            route_evidence.routing_decision,
            event_type="chat_completion_denied",
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

    forwarded = dict(payload)
    # Verdict-local controls must never be forwarded to an upstream provider.
    for local_field in ("request_id", "correlation_id", "criticality"):
        forwarded.pop(local_field, None)
    forwarded["model"] = decision.model

    started_at = monotonic()
    try:
        result = await proxy_instance.chat(forwarded)
    except asyncio.CancelledError:
        outcome = build_outcome_event(
            route_evidence.routing_decision,
            event_type="chat_completion_cancelled",
            outcome="cancelled",
            features=features,
            abort_observed=True,
            latency_ms=(monotonic() - started_at) * 1000,
        )
        _finish_evidence(route_evidence, outcome, evidence_key)
        raise
    except Exception as exc:
        evidence = _finish_evidence(
            route_evidence,
            build_outcome_event(
                route_evidence.routing_decision,
                event_type="chat_completion_error",
                outcome="error",
                features=features,
                error_class=type(exc).__name__,
                latency_ms=(monotonic() - started_at) * 1000,
            ),
            evidence_key,
        )
        return _proxy_error(
            502,
            "upstream request failed",
            extra={
                "decision": _safe_decision_dict(
                    replace(decision, transport_outcome="upstream_error")
                ),
            },
            headers=_evidence_headers(evidence),
        )

    transport_outcome = "success" if result.status_code < 400 else "upstream_error"
    decision_record = replace(decision, transport_outcome=transport_outcome)
    response_outcome = "success" if result.status_code < 400 else "error"
    response_headers = dict(result.headers)
    response_headers["x-verdict-model"] = decision_record.model
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
                event_type="chat_completion_buffered",
                outcome=response_outcome,
                status_code=result.status_code,
                features=features,
                latency_ms=(monotonic() - started_at) * 1000,
            ),
            evidence_key,
        )
        if result.status_code >= 400:
            return Response(
                content=result.body,
                status_code=result.status_code,
                headers=response_headers,
            )
        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=response_headers,
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
                details=details,
            )

        evidence_stream = _EvidenceStreamAdapter(
            result.body,
            on_terminal=finalize_stream,
            event_factory=stream_event,
        )

        return _EvidenceStreamingResponse(
            evidence_stream,
            status_code=result.status_code,
            headers=response_headers,
            media_type=None,
        )
    raise TypeError(f"unsupported upstream response: {type(result)!r}")


def _start_evidence(
    decision: Any,
    *,
    task: str,
    criticality: str,
    features: dict[str, Any],
    request_id: str | None = None,
    correlation_id: str | None = None,
    scope: str,
) -> tuple[ExplainEvidence, str | None]:
    """Create and retain immutable decision evidence before upstream I/O."""

    routing = build_routing_decision_contract(
        decision,
        task=task,
        criticality=criticality,
        features=features,
        request_id=request_id,
        correlation_id=correlation_id,
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
        stored = evidence_store_instance.update_outcome(evidence_key, event)
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
