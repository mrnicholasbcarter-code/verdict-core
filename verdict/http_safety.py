"""Fail-closed urllib transport for requests that can carry credentials."""

from __future__ import annotations

import urllib.error
import urllib.request
from http.client import HTTPResponse
from typing import Any, cast


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


def open_no_redirect(request: urllib.request.Request, *, timeout: float) -> HTTPResponse:
    """Open once, refusing all 3xx without forwarding any request headers.

    Non-redirect HTTP errors and transport errors retain their usual types.
    The opener is private; the process-wide urllib default is not modified.
    """
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        return cast(HTTPResponse, opener.open(request, timeout=timeout))  # nosec B310
    except urllib.error.HTTPError as exc:
        if not 300 <= exc.code < 400:
            raise
        code = exc.code
        exc.close()
        raise RedirectRefused(code) from None
