"""Detect a ``verdict serve`` refusal of an attempt's assigned route.

OpenCode reports an HTTP 503 from the gateway as ``retry`` events and keeps
retrying. When the 503 text names the attempt's own route as denied, retrying
cannot help, so the attempt should end early. Pure functions and a small
buffer: no I/O, no asyncio.
"""

from __future__ import annotations

import json
import re

from verdict.free_tier_admit import REASON_METADATA_GHOST, REASON_OPAQUE_ROUTE_DISALLOWED

PERMANENT_DENIAL_CODES: frozenset[str] = frozenset(
    {REASON_OPAQUE_ROUTE_DISALLOWED, REASON_METADATA_GHOST}
)

_MAX_PARTIAL_LINE_BYTES = 1024 * 1024


def _denial_text(route_id: str, code: str) -> str:
    """Serve's own words, up to the code (``IntelligenceService._assigned_denial_reason`` writes
    the route with ``!r``, so a route id is quoted exactly as Python quotes it)."""
    return f"assigned route {route_id!r} denied: {code}"


def _denial_pattern(route_id: str) -> re.Pattern[str]:
    # The code is lower-case snake case (``REASON_*``) and ends where serve's " (detail)" or the
    # end of the message begins; a letter or digit straight after it means it is not a code.
    prefix = re.escape(_denial_text(route_id, ""))
    return re.compile(rf"{prefix}([a-z0-9_]+)(?![A-Za-z0-9_])")


def _match_code(pattern: re.Pattern[str], text: object) -> str | None:
    if not isinstance(text, str):
        return None
    found = pattern.search(text)
    return found.group(1) if found else None


def serve_denial_code(line: str | bytes, route_id: str) -> str | None:
    """Return the denial code if ``line`` is a 503 retry denying ``route_id``.

    The text is looked for in ``error.message`` and, failing that, in ``error.response.body``
    (OpenCode copies the body's ``error.message`` into the first; the second is the raw body).
    """
    pattern = _denial_pattern(route_id)  # ``re`` keeps the compiled pattern for the next line
    try:
        event = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or event.get("type") != "retry":
        return None
    error = event.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status")
    if isinstance(status, bool) or status != 503:
        return None
    code = _match_code(pattern, error.get("message"))
    if code is not None:
        return code
    response = error.get("response")
    if isinstance(response, dict):
        return _match_code(pattern, response.get("body"))
    return None


class DenialWatch:
    """Watch OpenCode NDJSON output and say when the attempt should end."""

    def __init__(self, route_id: str) -> None:
        self._route_id = route_id
        self._buffer = b""
        self._skipping = False
        self._counts: dict[str, int] = {}
        self._done = False

    def feed(self, chunk: bytes) -> str | None:
        """Add output bytes; return the denial text the first time to end."""
        if self._done:
            return None
        self._buffer += chunk
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line = self._buffer[:newline]
            self._buffer = self._buffer[newline + 1 :]
            if self._skipping:
                self._skipping = False
                continue
            text = self._handle_line(line)
            if text is not None:
                self._done = True
                self._buffer = b""
                return text
        if len(self._buffer) > _MAX_PARTIAL_LINE_BYTES:
            self._buffer = b""
            self._skipping = True
        return None

    def _handle_line(self, line: bytes) -> str | None:
        code = serve_denial_code(line, self._route_id)
        if code is None:
            return None
        count = self._counts.get(code, 0) + 1
        self._counts[code] = count
        if code in PERMANENT_DENIAL_CODES or count >= 2:
            return _denial_text(self._route_id, code)
        return None
