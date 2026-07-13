import json
import os
from contextlib import asynccontextmanager
from typing import Any

try:
    from fastapi import FastAPI, HTTPException, Request
    from pydantic import BaseModel
    from starlette.responses import JSONResponse, Response, StreamingResponse
except ImportError as exc:
    raise ImportError(
        "FastAPI is required for the web server mode. Install with `pip install llm-gate[server]`"
    ) from exc

from llm_gate.gate import Gate
from llm_gate.proxy import BufferedUpstreamResponse, StreamedUpstreamResponse, UpstreamProxy

# Singleton Gate instance
gate_instance = None
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    global gate_instance, proxy_instance
    # Initialize the engine on startup
    gate_instance = Gate(
        primary_model=os.getenv("LLMGATE_PRIMARY", "anthropic/claude-3-opus-20240229"),
        log_path=os.getenv("LLMGATE_LOG_PATH", "llm-gate-decisions.jsonl"),
    )
    proxy_instance = _build_proxy()
    yield
    # Cleanup on shutdown
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


@app.post("/v1/route")
async def route_task(req: RouteRequest) -> dict[str, Any]:
    if not gate_instance:
        raise HTTPException(status_code=500, detail="Gate engine not initialized")

    decision = gate_instance.route(req.task, criticality=req.criticality)
    return decision.__dict__


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy", "engine": "llm-gate"}


@app.get("/ready")
async def ready() -> Response:
    """Report process readiness and verify that the configured upstream responds."""
    if gate_instance is None or proxy_instance is None:
        raise HTTPException(status_code=503, detail="Gate engine not initialized")
    try:
        upstream = await proxy_instance.models()
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "engine": "llm-gate",
                "upstream": proxy_instance.base_url,
                "reason": str(exc),
            },
        )
    if upstream.status_code >= 400:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "engine": "llm-gate",
                "upstream": proxy_instance.base_url,
                "reason": f"upstream returned HTTP {upstream.status_code}",
            },
        )
    return JSONResponse(
        content={"status": "ready", "engine": "llm-gate", "upstream": proxy_instance.base_url}
    )


def _proxy_error(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "type": "invalid_request_error"}},
    )


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


@app.get("/v1/models")
async def list_models() -> Response:
    """Pass through the configured upstream model catalog."""
    if proxy_instance is None:
        raise HTTPException(status_code=503, detail="Proxy not initialized")
    try:
        result = await proxy_instance.models()
    except Exception as exc:
        return _proxy_error(502, f"upstream model catalog unavailable: {exc}")
    return _as_response(result)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    """Route and transparently forward an OpenAI chat completion request."""
    if gate_instance is None or proxy_instance is None:
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
    decision = gate_instance.route(task, criticality="medium")
    forwarded = dict(payload)
    forwarded["model"] = decision.model

    try:
        result = await proxy_instance.chat(forwarded)
    except Exception as exc:
        return _proxy_error(502, f"upstream request failed: {exc}")

    response_headers = dict(result.headers)
    response_headers["x-llm-gate-model"] = decision.model
    response_headers["x-llm-gate-tier"] = str(decision.tier)
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
    return await route_task(req)


def start_server(port: int = 8000, host: str = "0.0.0.0") -> None:  # nosec B104
    """Boot the uvicorn server for the llm-gate microservice."""
    import uvicorn

    uvicorn.run(app, host=host, port=port)
