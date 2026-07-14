import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel
    from starlette.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the web server mode. Install with `pip install llm-gate[server]`"
    ) from exc

from llm_gate.catalog import configured_catalog_filters, normalize_catalog
from llm_gate.gate import Gate
from llm_gate.intelligence import DEFAULT_PROFILE, DEFAULT_TIMEOUT_MS, IntelligenceService
from llm_gate.models import ProviderConfig
from llm_gate.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy

# Singleton service instances
intelligence_instance: IntelligenceService | None = None
gate_instance: Gate | None = None
proxy_instance: UpstreamProxy | None = None

DEFAULT_UPSTREAM_BASE_URL = "http://127.0.0.1:20132/v1"
DEFAULT_MAX_REQUEST_BYTES = 2 * 1024 * 1024


def _build_proxy() -> UpstreamProxy:
    """Build the configured upstream transport without reading client fields."""
    base_url = os.getenv("LLMGATE_UPSTREAM_BASE_URL", DEFAULT_UPSTREAM_BASE_URL)
    api_key = os.getenv("LLMGATE_UPSTREAM_API_KEY") or os.getenv("OMNIROUTE_API_KEY")
    timeout_ms = int(os.getenv("LLMGATE_UPSTREAM_TIMEOUT_MS", "30000"))
    if timeout_ms <= 0:
        raise ValueError("LLMGATE_UPSTREAM_TIMEOUT_MS must be positive")
    return UpstreamProxy(base_url, api_key=api_key, timeout=timeout_ms / 1000)


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
    yield
    intelligence_instance = None
    gate_instance = None
    proxy_instance = None


app = FastAPI(
    title="llm-gate API",
    description="Microservice for Tier-based LLM Routing",
    version="0.2.0",
    lifespan=lifespan,
)


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
        "upstream": proxy_instance.base_url,
    }
    if upstream is not None:
        content["upstream_status_code"] = upstream.status_code
    if upstream_error:
        content["reason"] = upstream_error
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


@app.get("/v1/models")
async def list_models() -> Response:
    """Return a locally filtered catalog with conservative availability metadata."""
    if proxy_instance is None:
        raise HTTPException(status_code=503, detail="Proxy not initialized")
    try:
        result = await proxy_instance.models()
    except Exception as exc:
        return _proxy_error(502, f"upstream model catalog unavailable: {exc}")
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
    except Exception as exc:
        return _proxy_error(
            502,
            f"upstream request failed: {exc}",
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
        return json.loads(response.body.decode("utf-8"))
    return {"error": "unexpected response"}


def start_server(port: int = 8000, host: str = "0.0.0.0") -> None:  # nosec B104
    """Boot the uvicorn server for the llm-gate microservice."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
