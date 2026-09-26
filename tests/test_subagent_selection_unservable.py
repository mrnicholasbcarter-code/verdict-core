"""S2-A (D3): the live probe reads the error body and flags gateway-unservable ids."""

from __future__ import annotations

import io
import urllib.error
from typing import Any

from verdict.subagent_selection import LaunchCandidate, openai_health_probe


def _opener(body: bytes, code: int = 400) -> Any:
    def opener(request: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(request.full_url, code, "Bad Request", {}, io.BytesIO(body))  # type: ignore[arg-type]

    return opener


def _cand() -> LaunchCandidate:
    return LaunchCandidate("kr/x-xhigh", "kr/x-xhigh", frozenset(), 0, 0.0, 0.0, False, False, 0, 0)


def test_http_400_live_catalog_body_is_unservable() -> None:
    body = b'{"error":{"message":"kr/x-xhigh is not available in the active live catalog"}}'
    result = openai_health_probe("http://127.0.0.1:1/v1", opener=_opener(body))(_cand())
    assert result.healthy is False
    assert result.category == "unservable"
    assert result.status_code == 400


def test_http_400_other_body_is_unsupported() -> None:
    result = openai_health_probe("http://127.0.0.1:1/v1", opener=_opener(b"bad"))(_cand())
    assert result.category == "unsupported"


def test_http_402_is_payment_required() -> None:
    result = openai_health_probe("http://127.0.0.1:1/v1", opener=_opener(b"pay", 402))(_cand())
    assert result.category == "payment_required"
