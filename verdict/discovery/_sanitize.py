"""Shared exception-message sanitizer for the discovery package.

Both :mod:`verdict.discovery.collect` and
:mod:`scripts.refresh_discovery_sources` import
:func:`sanitize_exc_msg` from here so there is exactly one implementation.

:func:`~verdict.security.redact_text` is the primary redactor.  Two targeted
top-up patterns handle shapes it does not currently cover:

- URL query-string credential parameters (e.g. ``?key=…``, ``&api_key=…``).
- ``Authorization: Basic <token>`` headers (redact_text covers Bearer only).
"""

from __future__ import annotations

import re

# URL query-string credential params (e.g. ?key=…, &api_key=…).
# redact_text handles user:pass@ URLs but not ?name=value query params.
_URL_QUERY_CRED_RE = re.compile(
    r"([?&](?:api[_-]?key|token|key|secret|password|auth)[^=&]*=)[^\s&,;#]+", re.IGNORECASE
)

# Authorization: Basic <token>.
# redact_text's CREDENTIAL_SHAPES covers Bearer but not Basic.
_AUTH_BASIC_RE = re.compile(r"(?i)(authorization\s*:\s*basic\s+)[^\s,;]+")


def sanitize_exc_msg(exc: BaseException, *, limit: int = 200) -> str:
    """Return a sanitized, truncated exception message.

    Uses :func:`~verdict.security.redact_text` as the primary credential
    redactor, then applies two targeted top-up patterns for shapes that
    function does not cover.  The result is truncated to *limit* characters.

    Never includes a full traceback; callers that need the exception type
    should prepend it themselves (e.g. ``f"{type(exc).__name__}: {…}"``).

    Args:
        exc: The exception whose message to sanitize.
        limit: Maximum character length of the returned string.

    Returns:
        Sanitized, truncated exception message.
    """
    from verdict.security import redact_text

    sanitized = redact_text(str(exc))
    sanitized = _URL_QUERY_CRED_RE.sub(r"\1[redacted]", sanitized)
    sanitized = _AUTH_BASIC_RE.sub(r"\1[redacted]", sanitized)
    return sanitized[:limit]
