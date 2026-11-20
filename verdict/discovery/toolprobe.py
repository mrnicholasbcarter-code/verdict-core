"""verdict.discovery.toolprobe — second-stage tool-call check.

Ported from freescan/toolprobe.py (D1, BOD-342).

A route that says "ok" to a one-word prompt has not shown it can do agentic
work (BOD-299: 0 of 4 free-model sessions finished a task).  This sends each
working route one request with a single tool and checks the reply calls it
with the right arguments.

The request is padded to agent size (~18k tokens) because free tiers that
accept a tiny probe can reject a real agent request (Groq answered HTTP 413
to ~19 K-token requests).

Statuses: ``tool_ok``, ``no_tool_call`` (answered in text), ``bad_tool_call``
(wrong tool or arguments), ``tool_error`` (HTTP or transport failure).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from verdict.discovery import probe as _probe
from verdict.discovery.model import NO_KEY, UP, ProbeResult, Route

TOOL_OK = "tool_ok"
NO_TOOL_CALL = "no_tool_call"
BAD_TOOL_CALL = "bad_tool_call"
TOOL_ERROR = "tool_error"
TOOL_NOT_PROBED = "not_probed"  # deadline passed or stage skipped — no measurement made

TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}
PROMPT = "What is the weather in Paris right now? Use the get_weather tool."

# One line of plausible agent instructions, about 25 tokens.  Repeated to pad a
# request to agent size: free tiers that accept a tiny probe can reject a real
# agent request (Groq answered HTTP 413 to ~19K-token requests).
_FILLER = "Rule {n}: read the task, write a failing test first, change only the files you were given, then report what you ran.\n"


def filler(tokens: int) -> str:
    """About ``tokens`` tokens of numbered instruction lines (4 chars per token)."""
    out: list[str] = []
    n = 0
    while sum(map(len, out)) < tokens * 4:
        n += 1
        out.append(_FILLER.format(n=n))
    return "".join(out)


def classify(status_code: int, body: bytes) -> tuple[str, str | None]:
    """Classify a tool-probe response into one of the TOOL_* statuses."""
    if status_code != 200:
        return TOOL_ERROR, f"HTTP {status_code}"
    try:
        data = json.loads(body)
        message = data["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
    except Exception:
        return NO_TOOL_CALL, "could not parse response"
    if not tool_calls:
        return NO_TOOL_CALL, None
    fn = (tool_calls[0].get("function") or {}) if isinstance(tool_calls[0], dict) else {}
    if fn.get("name") != "get_weather":
        return BAD_TOOL_CALL, f"name={fn.get('name')!r}"
    raw = fn.get("arguments", "{}")
    try:
        args: object = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return BAD_TOOL_CALL, "arguments are not JSON"
    city = str((args or {}).get("city") or "") if isinstance(args, dict) else ""
    if "paris" not in city.lower():
        return BAD_TOOL_CALL, f"city={city!r}"
    return TOOL_OK, None


def tool_probe(
    route: Route, *, timeout: float = 30.0, max_tokens: int = 512, context_tokens: int = 0
) -> tuple[str, str | None, int | None, str | None]:
    """(status, detail, latency_ms, served) for one route.  Never raises.

    ``context_tokens`` pads the request with that many tokens of instructions.
    ``served`` is the upstream a gateway reports in its response headers.
    """
    auth_header, early = _probe.resolve_auth(route.auth)
    if early == NO_KEY:
        return TOOL_ERROR, "env var unset", None, None
    messages: list[dict[str, object]] = [{"role": "user", "content": PROMPT}]
    if context_tokens > 0:
        messages.insert(0, {"role": "system", "content": filler(context_tokens)})
    payload = json.dumps(
        {
            "model": route.model,
            "messages": messages,
            "tools": [TOOL],
            "tool_choice": "auto",
            "max_tokens": max_tokens,
        }
    ).encode()
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "verdict/0.1",
        **route.headers,
    }
    if auth_header is not None:
        headers["Authorization"] = auth_header
    url = route.base_url.rstrip("/") + "/chat/completions"
    started = time.monotonic()
    try:
        status_code, resp_headers, body = _probe.get_transport()(url, headers, payload, timeout)
    except Exception as exc:
        return TOOL_ERROR, type(exc).__name__, None, None
    elapsed = int((time.monotonic() - started) * 1000)
    status, detail = classify(status_code, body)
    h = {str(k).lower(): v for k, v in (resp_headers or {}).items()}
    served: str | None = None
    if h.get("x-omniroute-provider") and h.get("x-omniroute-model"):
        served = f"{h['x-omniroute-provider']}/{h['x-omniroute-model']}"
    return status, detail, elapsed, served


def tool_probe_all(
    results: list[ProbeResult],
    *,
    timeout: float = 30.0,
    concurrency: int = 48,
    per_host: int = 4,
    context_tokens: int = 0,
    deadline: float | None = None,
) -> dict[str, tuple[str, str | None, int | None, str | None]]:
    """Tool-probe every UP result; returns {route.key: (status, detail, ms, served)}.

    When ``deadline`` is set, each worker checks ``time.monotonic()`` before
    sending the request.  Routes that are reached after the deadline are marked
    ``(TOOL_NOT_PROBED, "wall_budget_exhausted", None, None)`` without making a
    network call, so a stale budget can never produce a measured failure.
    """
    todo = [r.route for r in results if r.status == UP]
    sems: dict[str, threading.Semaphore] = {}
    lock = threading.Lock()

    def host_sem(route: Route) -> threading.Semaphore:
        host = urllib.parse.urlparse(route.base_url).hostname or route.base_url
        if route.provider == "omniroute" and route.upstream:
            host = f"{host}|{route.upstream}"
        with lock:
            if host not in sems:
                cap = min(per_host, route.max_parallel) if route.max_parallel else per_host
                sems[host] = threading.Semaphore(max(1, cap))
            return sems[host]

    def run(route: Route) -> tuple[str, tuple[str, str | None, int | None, str | None]]:
        if deadline is not None and time.monotonic() >= deadline:
            return route.key, (TOOL_NOT_PROBED, "wall_budget_exhausted", None, None)
        with host_sem(route):
            return route.key, tool_probe(route, timeout=timeout, context_tokens=context_tokens)

    out: dict[str, tuple[str, str | None, int | None, str | None]] = {}
    if not todo:
        return out
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, len(todo)))) as pool:
        for key, value in pool.map(run, todo):
            out[key] = value
    return out
