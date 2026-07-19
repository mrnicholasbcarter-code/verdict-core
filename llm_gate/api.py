import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from typing import Any, cast

try:
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel
    from starlette.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the web server mode. Install with `pip install llm-gate[server]`"
    ) from exc

from llm_gate.availability import OmniRouteAvailabilityAdapter
from llm_gate.availability_cache import AvailabilityCache
from llm_gate.catalog import configured_catalog_filters, normalize_catalog
from llm_gate.eligibility import EligibilityGate
from llm_gate.gate import Gate
from llm_gate.intelligence import DEFAULT_PROFILE, DEFAULT_TIMEOUT_MS, IntelligenceService
from llm_gate.models import ModelInfo, ProviderConfig
from llm_gate.omniroute import OmniRouteHTTPTransport
from llm_gate.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy
from llm_gate.security import bearer_matches, redact_text, validate_server_security

# Singleton service instances
intelligence_instance: IntelligenceService | None = None
gate_instance: Gate | None = None
proxy_instance: UpstreamProxy | None = None
availability_cache_instance: AvailabilityCache | None = None
eligibility_gate_instance: EligibilityGate | None = None

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
        from llm_gate.availability import ProbeEnrichedAdapter
        from llm_gate.probes import openai_probe_transport

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
    from llm_gate.eligibility import EligibilityGate

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
        log_path=os.getenv("LLMGATE_LOG_PATH", "llm-gate-decisions.jsonl"),
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
    global intelligence_instance, gate_instance, proxy_instance
    intelligence_instance = _build_intelligence()
    gate_instance = Gate(
        primary_model=intelligence_instance.primary_model,
        providers=intelligence_instance.providers,
        log_path=intelligence_instance.log_path,
        log_full_task=intelligence_instance.log_full_task,
        discovery_ttl=intelligence_instance.discovery_ttl,
        profile=intelligence_instance.profile,
        intelligence_service=intelligence_instance,
    )
    proxy_instance = _build_proxy()
    global availability_cache_instance, eligibility_gate_instance
    built = _build_availability_cache()
    availability_cache_instance, eligibility_gate_instance = (
        built if built is not None else (None, None)
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


app = FastAPI(
    title="llm-gate API",
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


async def _route_with_intelligence(
    task: str,
    criticality: str,
    context: dict[str, Any] | None = None,
) -> Any:
    if intelligence_instance is None:
        raise HTTPException(status_code=503, detail="Intelligence service not initialized")
    return intelligence_instance.route(task, criticality=criticality, context=context)


@app.post("/v1/route")
async def route_task(req: RouteRequest) -> Response:
    context = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    decision = await _route_with_intelligence(req.task, req.criticality, context=context)
    status_code = 200 if decision.decision != "denied" else 503
    return JSONResponse(content=asdict(decision), status_code=status_code)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "engine": "llm-gate"}


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
        "engine": "llm-gate",
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
    status_code: int, message: str, *, extra: dict[str, Any] | None = None
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"message": message, "type": "invalid_request_error"}}
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=status_code, content=payload)


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


@app.get("/v1/route/explain")
async def route_explain(model_id: str | None = None) -> Response:
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
    if availability_cache_instance is None:
        return _proxy_error(
            503,
            "availability cache not configured (set OMNIROUTE_BASE_URL to enable)",
        )
    if model_id is None or model_id == "":
        base: dict[str, Any] = {
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
    record = availability_cache_instance.explain(model_id)
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
    decision = intelligence_instance.route(
        task, criticality=payload.get("criticality", "medium"), context=payload
    )
    if decision.decision == "denied":
        return _proxy_error(503, decision.reason, extra={"decision": asdict(decision)})

    forwarded = dict(payload)
    forwarded["model"] = decision.model

    try:
        result = await proxy_instance.chat(forwarded)
    except Exception:
        return _proxy_error(
            502,
            "upstream request failed",
            extra={"decision": asdict(replace(decision, transport_outcome="upstream_error"))},
        )

    transport_outcome = "success" if result.status_code < 400 else "upstream_error"
    decision_record = replace(decision, transport_outcome=transport_outcome)
    response_headers = dict(result.headers)
    response_headers["x-llm-gate-model"] = decision_record.model
    response_headers["x-llm-gate-tier"] = str(decision_record.tier)
    response_headers["x-llm-gate-request-id"] = decision_record.request_id
    response_headers["x-llm-gate-decision"] = decision_record.decision
    response_headers["x-llm-gate-transport-outcome"] = decision_record.transport_outcome
    response_headers["x-llm-gate-quality-outcome"] = decision_record.quality_outcome
    response_headers["x-llm-gate-degraded-mode"] = str(decision_record.degraded_mode).lower()

    if isinstance(result, BufferedUpstreamResponse):
        return Response(
            content=result.body,
            status_code=result.status_code,
            headers=response_headers,
        )
    if isinstance(result, StreamedUpstreamResponse):
        return StreamingResponse(
            result.body,
            status_code=result.status_code,
            headers=response_headers,
            media_type=None,
        )
    raise TypeError(f"unsupported upstream response: {type(result)!r}")


@app.post("/route")
async def route_task_alias(req: RouteRequest) -> dict[str, Any]:
    """Convenience alias matching the integration test client path."""
    response = await route_task(req)
    if isinstance(response, JSONResponse):
        body: bytes = bytes(response.body)
        return dict(json.loads(body.decode("utf-8")))
    return {"error": "unexpected response"}


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
