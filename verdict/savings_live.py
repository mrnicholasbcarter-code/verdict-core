"""OmniRoute-backed ``execute_arm`` for the live paired savings bench (BOD-114).

This is the only code path that can produce a claimable savings report, and it
never runs implicitly: ``verdict benchmark --savings --live-paired`` requires
``OMNIROUTE_BASE_URL`` (and usually ``OMNIROUTE_API_KEY``) to be present, and
each arm is a real ``POST /v1/chat/completions`` against that gateway.

Evidence returned per arm (see :class:`verdict.savings_execution.ArmExecution`):

* ``execution_id`` — ``X-OmniRoute-Request-Id`` (or the response body ``id``),
* ``completed_with`` — ``X-OmniRoute-Model`` / ``X-OmniRoute-Completed-With``
  **only**. The body ``model`` is an echo of the request alias and is never
  treated as provider-bound identity; without the header the arm is
  ``identity_unbound`` and cannot claim,
* ``input_hash`` — recomputed from the exact bytes this client transmitted
  (:func:`verdict.savings_execution.input_hash_from_sent_payload`), never
  copied from the request,
* ``attempt_chain`` — ``X-OmniRoute-Attempt-Chain`` (comma-separated) when the
  gateway reports one; otherwise empty. The *planned* chain on the request is
  never reported as observed,
* cost/tokens/cache — the raw ``x-omniroute-*`` headers, consumed by
  :func:`verdict.savings_bench.parse_measured_cost`; nothing is estimated,
* the produced output text (digested in the evidence bundle, never stored).

Nothing here is exercised in CI: there are no gateway credentials in CI and a
recorded response would not be an execution.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any

import httpx

from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.savings_execution import (
    ArmExecution,
    ArmExecutor,
    ArmRequest,
    input_hash_from_sent_payload,
)

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_TOKENS = 1024
GATEWAY = "omniroute"
_ID_HEADERS = ("x-omniroute-request-id", "x-request-id")
_MODEL_HEADERS = ("x-omniroute-model", "x-omniroute-completed-with")
_CHAIN_HEADERS = ("x-omniroute-attempt-chain", "x-omniroute-fallback-chain")


class LiveExecutorUnavailableError(RuntimeError):
    """Raised when live paired execution was requested without a configured gateway."""


def _content_of(body: Mapping[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], Mapping) else {}
    message = first.get("message") if isinstance(first, Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def _first_header(headers: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {key.lower(): value for key, value in headers.items()}
    for name in names:
        value = lowered.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def omniroute_arm_executor(
    base_url: str,
    api_key: str | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    transport: httpx.BaseTransport | None = None,
) -> ArmExecutor:
    """Build an executor that runs each arm against OmniRoute and returns raw evidence."""
    origin = normalize_omniroute_origin(base_url)
    url = f"{origin}/v1/chat/completions"
    headers = {"content-type": "application/json", "accept": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    def execute(request: ArmRequest) -> ArmExecution:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": request.messages(),
            "max_tokens": max_tokens,
            "stream": False,
        }
        # Serialize once and send those exact bytes so the input hash we report
        # describes what actually left this process, not a re-encoding of it.
        content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with httpx.Client(timeout=timeout, transport=transport, follow_redirects=False) as client:
            response = client.post(url, headers=headers, content=content)
        try:
            body = response.json()
        except ValueError:
            body = {}
        body_map: Mapping[str, Any] = body if isinstance(body, Mapping) else {}
        response_headers = {key.lower(): value for key, value in response.headers.items()}
        execution_id = _first_header(response_headers, _ID_HEADERS) or str(body_map.get("id") or "")
        # Identity is provider-bound only when the gateway asserts it in a header.
        # ``body.model`` echoes the requested alias and proves nothing.
        completed_with = _first_header(response_headers, _MODEL_HEADERS) or ""
        chain_header = _first_header(response_headers, _CHAIN_HEADERS)
        observed_chain = (
            tuple(part.strip() for part in chain_header.split(",") if part.strip())
            if chain_header
            else ()
        )
        raw_usage = body_map.get("usage")
        usage: dict[str, Any] = dict(raw_usage) if isinstance(raw_usage, Mapping) else {}
        receipt: dict[str, Any] = {
            "status_code": response.status_code,
            "usage": usage,
            "identity_source": "header" if completed_with else "none",
            "input_hash_source": "sent_payload",
            "sent_payload_digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
            "attempt_chain_source": "header" if observed_chain else "none",
        }
        return ArmExecution(
            arm=request.arm,
            execution_id=execution_id,
            input_hash=input_hash_from_sent_payload(content),
            completed_with=completed_with,
            gateway=GATEWAY,
            output=_content_of(body_map),
            headers=response_headers,
            receipt=receipt,
            attempt_chain=observed_chain,
            status_code=response.status_code,
        )

    return execute


def executor_from_env(env: Mapping[str, str] | None = None) -> ArmExecutor:
    """Executor from ``OMNIROUTE_BASE_URL`` / ``OMNIROUTE_API_KEY``; fail closed if absent."""
    source = os.environ if env is None else env
    base_url = (source.get("OMNIROUTE_BASE_URL") or "").strip()
    if not base_url:
        raise LiveExecutorUnavailableError(
            "live paired execution requires OMNIROUTE_BASE_URL (and usually OMNIROUTE_API_KEY); "
            "without a gateway the bench runs as a labeled simulation and cannot claim savings"
        )
    return omniroute_arm_executor(base_url, source.get("OMNIROUTE_API_KEY") or None)


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_TIMEOUT_SECONDS",
    "GATEWAY",
    "LiveExecutorUnavailableError",
    "executor_from_env",
    "omniroute_arm_executor",
]
