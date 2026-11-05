"""Fail-closed urllib transport for requests that can carry credentials.

Also the one shared place for OmniRoute evidence safety (BOD-219): the
per-request opt-out headers every Verdict-owned inference request sends, and
the response-header check that names a reply OmniRoute altered or faked.
"""

from __future__ import annotations

import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from http.client import HTTPResponse
from types import MappingProxyType
from typing import Any, cast

#: OmniRoute per-request opt-outs. Without them OmniRoute may answer a
#: ``temperature: 0`` request from its exact-match cache, compress messages and
#: tool results, or inject memory/skills, so the reply is no longer evidence of
#: the requested route. Read-only so no caller can weaken it in place.
OMNIROUTE_EVIDENCE_HEADERS = MappingProxyType(
    {
        "X-OmniRoute-No-Cache": "true",
        "X-OmniRoute-Compression": "off",
        "X-OmniRoute-No-Memory": "true",
    }
)

ANOMALY_CACHE_HIT = "cache_hit"
ANOMALY_COMPRESSED = "compressed"
ANOMALY_EMERGENCY_FALLBACK = "emergency_fallback"
#: Anomalies that void a reply as proof: a replayed answer, or another model.
#: ``compressed`` is reported but does not void proof on its own.
_PROOF_VOIDING_ANOMALIES = (ANOMALY_CACHE_HIT, ANOMALY_EMERGENCY_FALLBACK)
#: Every reason :func:`untrusted_response_reason` can return.
UNTRUSTED_RESPONSE_REASONS = frozenset(f"omniroute_{name}" for name in _PROOF_VOIDING_ANOMALIES)


def omniroute_evidence_headers() -> dict[str, str]:
    """Return the evidence opt-outs plus a fresh ``X-Request-Id`` for one request."""
    return {**OMNIROUTE_EVIDENCE_HEADERS, "X-Request-Id": str(uuid.uuid4())}


# Fail closed: a marker header is safe only when absent or carrying the exact
# value OmniRoute 3.8.53 sends for "nothing happened". Any other value, unknown
# text included, is an anomaly. Values from the 3.8.53 package source:
# - X-OmniRoute-Cache: "MISS" (open-sse/handlers/chatCore/responseHeaders.ts:345,
#   nonStreamingResponseHeaders.ts:33) or "HIT" (chatCore/semanticCache.ts:174).
#   There is no other value, so "BYPASS" is unknown and unsafe.
# - X-OmniRoute-Cache-Hit: String(cacheHit), "true"/"false"
#   (src/domain/omnirouteResponseMeta.ts:162).
# - X-OmniRoute-Compression: "<mode>; source=<source>"; mode "off" renders as
#   "off; source=request-header" (open-sse/services/compression/planResolution.ts:41,67;
#   src/shared/utils/compressionHeaderEcho.ts:53).
# - X-OmniRoute-Emergency-Fallback: "from=<provider/model>; to=<provider/model>",
#   only when the fallback served the reply (src/sse/handlers/emergencyFallbackHeader.ts:34).
def _marker_is_safe(values: list[str] | None, is_safe: Callable[[str], bool]) -> bool:
    return values is None or all(is_safe(value.strip()) for value in values)


def _compression_is_off(value: str) -> bool:
    return value.split(";", 1)[0].strip() == "off"


def omniroute_response_anomalies(headers: Any) -> tuple[str, ...]:
    """Name what OmniRoute's response headers say it did to this reply.

    ``headers`` is any object with ``items()`` (``dict``, ``httpx.Headers``,
    ``http.client.HTTPMessage``) or ``None``. Header names match
    case-insensitively; values must be the exact safe value (see above), and a
    repeated header is safe only when every value is. Returns a subset of
    ``cache_hit``, ``compressed``, ``emergency_fallback`` in that order; an
    empty tuple means every marker was absent or safe.
    """
    items = getattr(headers, "items", None)
    if not callable(items):
        return ()
    values: dict[str, list[str]] = {}
    for key, value in items():
        values.setdefault(str(key).lower(), []).append(str(value))
    found: list[str] = []
    if not (
        _marker_is_safe(values.get("x-omniroute-cache"), lambda value: value == "MISS")
        and _marker_is_safe(values.get("x-omniroute-cache-hit"), lambda value: value == "false")
    ):
        found.append(ANOMALY_CACHE_HIT)
    if not _marker_is_safe(values.get("x-omniroute-compression"), _compression_is_off):
        found.append(ANOMALY_COMPRESSED)
    if not _marker_is_safe(
        values.get("x-omniroute-emergency-fallback"), lambda value: value == "false"
    ):
        found.append(ANOMALY_EMERGENCY_FALLBACK)
    return tuple(found)


def untrusted_response_reason(anomalies: Iterable[str]) -> str | None:
    """Return ``omniroute_<anomaly>`` when a reply must not count as proof, else None."""
    present = set(anomalies)
    for anomaly in _PROOF_VOIDING_ANOMALIES:
        if anomaly in present:
            return f"omniroute_{anomaly}"
    return None


class RedirectRefused(urllib.error.HTTPError):
    """A refused redirect, compatible with existing HTTPError handlers.

    Do not retain the request URL, response body, or redirect headers: any of
    those may contain credentials echoed by an untrusted server.
    """

    def __init__(self, code: int) -> None:
        from email.message import Message

        super().__init__("", code, "redirect refused", Message(), None)

    def __str__(self) -> str:
        return f"HTTP Error {self.code}: redirect refused"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class _RefuseRedirectResponse(urllib.request.HTTPErrorProcessor):
    def http_response(
        self, request: urllib.request.Request, response: HTTPResponse
    ) -> HTTPResponse:
        # Refuse before urllib parses Location, which may itself be malformed
        # or contain a credential. All 3xx stay on the caller's HTTPError path.
        if 300 <= response.status < 400:
            code = response.status
            response.close()
            raise RedirectRefused(code)
        return cast(HTTPResponse, super().http_response(request, response))

    https_response = http_response


def open_no_redirect(request: urllib.request.Request, *, timeout: float) -> HTTPResponse:
    """Open once, refusing all 3xx without forwarding any request headers.

    Non-redirect HTTP errors and transport errors retain their usual types.
    The opener is private; the process-wide urllib default is not modified.
    """
    opener = urllib.request.build_opener(_NoRedirect(), _RefuseRedirectResponse())
    return cast(HTTPResponse, opener.open(request, timeout=timeout))  # nosec B310
