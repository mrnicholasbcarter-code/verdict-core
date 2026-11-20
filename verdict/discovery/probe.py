"""verdict.discovery.probe — parallel probe of OpenAI-compatible endpoints.

Ported from freescan/probe.py (D1, BOD-342).

Transport is injectable (``_set_transport``) so tests never make real network calls.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime

from verdict.discovery.model import (
    AUTH_ERROR,
    AUTH_NONE,
    DOWN,
    ERROR,
    GARBAGE,
    NO_KEY,
    RATE_LIMITED,
    TIMEOUT,
    UP,
    ProbeResult,
    Route,
)
from verdict.http_safety import open_no_redirect

# ---------------------------------------------------------------------------
# Injectable transport — overridden in tests to avoid any network call
# ---------------------------------------------------------------------------

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, str], bytes]]

_TRANSPORT: Transport | None = None
_TRANSPORT_LOCK = threading.Lock()


def _set_transport(fn: Transport | None) -> None:
    """Replace the HTTP backend.  Pass None to restore the real urllib backend."""
    global _TRANSPORT
    with _TRANSPORT_LOCK:
        _TRANSPORT = fn


def _get_transport() -> Transport:
    with _TRANSPORT_LOCK:
        return _TRANSPORT if _TRANSPORT is not None else _real_http


def _real_http(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, dict[str, str], bytes]:
    """Real HTTP POST via open_no_redirect; returns (status, headers_dict, body_bytes)."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with open_no_redirect(req, timeout=timeout) as resp:
            return int(resp.status), dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        body_bytes = b""
        import contextlib

        with contextlib.suppress(Exception):
            body_bytes = exc.read()
        return exc.code, dict(exc.headers), body_bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_RATE_RE = re.compile(r"(rate.?limit|quota|too.many.request|throttl|credits?.exhaust)", re.I)


def _parse_retry_after(value: str) -> float | None:
    """Parse a Retry-After header value (seconds int or HTTP-date)."""
    value = value.strip()
    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
        now = datetime.datetime.now(datetime.timezone.utc)
        return max(0.0, (dt - now).total_seconds())
    except Exception:
        return None


def _resolve_auth(auth: str) -> tuple[str | None, str]:
    """Return (Authorization header value or None, early-status or '').

    Returns (None, NO_KEY) when an env-var key is not set; in that case the
    caller must NOT make a network call.
    """
    if auth == AUTH_NONE:
        return None, ""
    if auth.startswith("literal:"):
        return f"Bearer {auth[len('literal:') :]}", ""
    if auth.startswith("env:"):
        name = auth[len("env:") :]
        value = os.environ.get(name)
        if not value:
            return None, NO_KEY
        return f"Bearer {value}", ""
    return None, ""


def _sanitize(text: str | None, auth: str) -> str | None:
    """Strip credential values from a human-visible string."""
    if text is None:
        return None
    if auth.startswith("literal:"):
        secret = auth[len("literal:") :]
        if secret:
            text = text.replace(secret, "***")
    if auth.startswith("env:"):
        name = auth[len("env:") :]
        secret = os.environ.get(name, "")
        if secret:
            text = text.replace(secret, "***")
    return text[:200]


def _upstream_error(body_obj: dict[str, object]) -> str | None:
    """Extract the first useful message from an upstream JSON error body."""
    err = body_obj.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("msg") or ""
        return str(msg)[:200] or None
    if isinstance(err, str):
        return err[:200]
    return None


def _has_usable_content(choice: dict[str, object]) -> bool:
    """True when a choices[0] carries content that proves the model answered."""
    msg = choice.get("message") or {}
    if not isinstance(msg, dict):
        return False
    # Non-empty text content
    content = msg.get("content")
    if content and str(content).strip():
        return True
    # Tool calls
    if msg.get("tool_calls"):
        return True
    # Reasoning-only models (counts as alive per spec)
    for key in ("reasoning", "reasoning_content"):
        v = msg.get(key)
        if v and str(v).strip():
            return True
    return False


# ---------------------------------------------------------------------------
# Single-route probe
# ---------------------------------------------------------------------------


def probe_one(route: Route, *, timeout: float = 12.0, max_tokens: int = 256) -> ProbeResult:
    """Probe one route and return a classified ProbeResult."""
    auth = route.auth
    auth_header, early = _resolve_auth(auth)
    if early == NO_KEY:
        return ProbeResult(route=route, status=NO_KEY, detail="env var unset")

    url = route.base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps(
        {
            "model": route.model,
            "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
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

    t0 = time.monotonic()
    transport = _get_transport()

    try:
        status_code, resp_headers, body_bytes = transport(url, headers, payload, timeout)
    except TimeoutError:
        return ProbeResult(route=route, status=TIMEOUT, detail="request timed out")
    except OSError as exc:
        if isinstance(exc, socket.timeout):
            return ProbeResult(route=route, status=TIMEOUT, detail="request timed out")
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, socket.timeout):
            return ProbeResult(route=route, status=TIMEOUT, detail="request timed out")
        return ProbeResult(route=route, status=ERROR, detail=_sanitize(str(exc), auth))
    except Exception as exc:
        return ProbeResult(route=route, status=ERROR, detail=_sanitize(str(exc), auth))

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Normalise header keys to lowercase for consistent lookup
    _h = {k.lower(): v for k, v in resp_headers.items()}

    retry_after_s: float | None = None
    if "retry-after" in _h:
        retry_after_s = _parse_retry_after(_h["retry-after"])

    # --- classify by HTTP status ---
    if status_code in (401, 403):
        try:
            detail: str | None = _upstream_error(json.loads(body_bytes))
        except Exception:
            detail = body_bytes[:200].decode("utf-8", errors="replace") or None
        return ProbeResult(
            route=route,
            status=AUTH_ERROR,
            latency_ms=latency_ms,
            http_code=status_code,
            detail=_sanitize(detail, auth),
            retry_after_s=retry_after_s,
        )

    if status_code == 429:
        try:
            detail = _upstream_error(json.loads(body_bytes))
        except Exception:
            detail = body_bytes[:200].decode("utf-8", errors="replace") or None
        return ProbeResult(
            route=route,
            status=RATE_LIMITED,
            latency_ms=latency_ms,
            http_code=status_code,
            detail=_sanitize(detail, auth),
            retry_after_s=retry_after_s,
        )

    if status_code != 200:
        try:
            body_obj = json.loads(body_bytes)
            detail2: str | None = _upstream_error(body_obj) or body_bytes[:200].decode(
                "utf-8", errors="replace"
            )
        except Exception:
            detail2 = body_bytes[:200].decode("utf-8", errors="replace") if body_bytes else None
        if detail2 and _RATE_RE.search(detail2):
            return ProbeResult(
                route=route,
                status=RATE_LIMITED,
                latency_ms=latency_ms,
                http_code=status_code,
                detail=_sanitize(detail2, auth),
                retry_after_s=retry_after_s,
            )
        return ProbeResult(
            route=route,
            status=DOWN,
            latency_ms=latency_ms,
            http_code=status_code,
            detail=_sanitize(detail2, auth),
            retry_after_s=retry_after_s,
        )

    # --- HTTP 200 ---
    ct = _h.get("content-type", "")
    if "text/html" in ct.lower():
        return ProbeResult(
            route=route,
            status=GARBAGE,
            latency_ms=latency_ms,
            http_code=200,
            detail="HTML response body",
        )

    try:
        body_obj = json.loads(body_bytes)
    except Exception:
        snippet = body_bytes[:100].decode("utf-8", errors="replace")
        if "<html" in snippet.lower() or "<!doctype" in snippet.lower():
            return ProbeResult(
                route=route,
                status=GARBAGE,
                latency_ms=latency_ms,
                http_code=200,
                detail="HTML response body",
            )
        return ProbeResult(
            route=route,
            status=GARBAGE,
            latency_ms=latency_ms,
            http_code=200,
            detail="non-JSON response body",
        )

    answered_model: str | None = body_obj.get("model") if isinstance(body_obj, dict) else None
    # A gateway combo answers under the combo's name; OmniRoute names the real
    # upstream in response headers, which is what a per-model measurement needs.
    if _h.get("x-omniroute-provider") and _h.get("x-omniroute-model"):
        answered_model = f"{_h['x-omniroute-provider']}/{_h['x-omniroute-model']}"

    if not isinstance(body_obj, dict):
        return ProbeResult(
            route=route,
            status=GARBAGE,
            latency_ms=latency_ms,
            http_code=200,
            detail="non-object JSON body",
        )

    # Error object inside a 200
    if "error" in body_obj:
        detail3: str | None = _upstream_error(body_obj)
        if detail3 and _RATE_RE.search(detail3):
            return ProbeResult(
                route=route,
                status=RATE_LIMITED,
                latency_ms=latency_ms,
                http_code=200,
                detail=_sanitize(detail3, auth),
                retry_after_s=retry_after_s,
                answered_model=answered_model,
            )
        return ProbeResult(
            route=route,
            status=GARBAGE,
            latency_ms=latency_ms,
            http_code=200,
            detail=_sanitize(detail3 or "error object in 200 response", auth),
            answered_model=answered_model,
        )

    choices = body_obj.get("choices")
    if not choices or not isinstance(choices, list):
        return ProbeResult(
            route=route,
            status=GARBAGE,
            latency_ms=latency_ms,
            http_code=200,
            detail="empty or missing choices",
            answered_model=answered_model,
        )

    first = choices[0]
    if not isinstance(first, dict):
        return ProbeResult(
            route=route,
            status=GARBAGE,
            latency_ms=latency_ms,
            http_code=200,
            detail="first choice is not an object",
            answered_model=answered_model,
        )

    if _has_usable_content(first):
        return ProbeResult(
            route=route,
            status=UP,
            latency_ms=latency_ms,
            http_code=200,
            answered_model=answered_model,
        )

    return ProbeResult(
        route=route,
        status=GARBAGE,
        latency_ms=latency_ms,
        http_code=200,
        detail="first choice has no usable content",
        answered_model=answered_model,
    )


# ---------------------------------------------------------------------------
# Parallel fan-out
# ---------------------------------------------------------------------------


def probe_all(
    routes: list[Route],
    *,
    timeout: float = 12.0,
    concurrency: int = 48,
    per_host: int = 8,
    max_tokens: int = 256,
) -> list[ProbeResult]:
    """Probe all routes in parallel, return results in the same order as input.

    Never raises; unexpected exceptions become ERROR results.
    """
    if not routes:
        return []

    _sems: dict[str, threading.Semaphore] = {}
    _sems_lock = threading.Lock()

    def _host_sem(route: Route) -> threading.Semaphore:
        host = urllib.parse.urlparse(route.base_url).hostname or route.base_url
        # Every OmniRoute route shares one local host; cap per upstream provider
        # instead, so hundreds of gateway routes do not queue behind a small slot count.
        if route.provider == "omniroute" and route.upstream:
            host = f"{host}|{route.upstream}"
        with _sems_lock:
            if host not in _sems:
                cap = min(per_host, route.max_parallel) if route.max_parallel else per_host
                _sems[host] = threading.Semaphore(max(1, cap))
            return _sems[host]

    def _safe_probe(route: Route) -> ProbeResult:
        sem = _host_sem(route)
        with sem:
            try:
                return probe_one(route, timeout=timeout, max_tokens=max_tokens)
            except Exception as exc:
                return ProbeResult(route=route, status=ERROR, detail=str(exc)[:200])

    workers = min(concurrency, len(routes))
    results: list[ProbeResult | None] = [None] * len(routes)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_safe_probe, r): i for i, r in enumerate(routes)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except Exception as exc:
                results[idx] = ProbeResult(route=routes[idx], status=ERROR, detail=str(exc)[:200])

    # Fallback for any slot that somehow wasn't filled
    return [
        r if r is not None else ProbeResult(route=routes[i], status=ERROR, detail="unknown")
        for i, r in enumerate(results)
    ]
