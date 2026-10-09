"""Authenticated urllib calls must never forward a credential to a redirect target."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from verdict.http_safety import RedirectRefused, open_no_redirect


@contextmanager
def server(status: int, seen: list[str | None], *, location: str | None = None) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self) -> None:
            self.do_GET()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_redirect_never_reaches_target(status: int, method: str) -> None:
    key = "secret-redirect-test-key"
    source_seen: list[str | None] = []
    target_seen: list[str | None] = []
    error: urllib.error.HTTPError | None = None
    with server(200, target_seen) as target:
        destination = target.replace("127.0.0.1", "localhost") + f"/?key={key}"
        with server(status, source_seen, location=destination) as source:
            request = urllib.request.Request(
                source, headers={"Authorization": f"Bearer {key}"}, method=method
            )
            try:
                with open_no_redirect(request, timeout=2):
                    pass
            except urllib.error.HTTPError as exc:
                error = exc
                exc.close()
    assert source_seen == [f"Bearer {key}"]
    assert target_seen == []
    assert isinstance(error, RedirectRefused) and error.code == status
    assert key not in str(error)
    assert source not in str(error) and destination not in str(error)


@pytest.mark.parametrize("status", [200, 401, 403, 429, 500])
def test_preserves_normal_response_and_error(status: int) -> None:
    seen: list[str | None] = []
    with server(status, seen) as url:
        request = urllib.request.Request(url, headers={"Authorization": "Bearer test-key"})
        if status == 200:
            with open_no_redirect(request, timeout=2) as response:
                assert response.status == status
                assert response.read() == b"{}"
        else:
            with pytest.raises(urllib.error.HTTPError) as raised:
                open_no_redirect(request, timeout=2)
            assert raised.value.code == status
            assert raised.value.read() == b"{}"
            raised.value.close()
    assert seen == ["Bearer test-key"]


@pytest.mark.parametrize("location", [None, "https://[invalid", "file:///secret-key"])
def test_redirect_refusal_ignores_untrusted_location(location: str | None) -> None:
    seen: list[str | None] = []
    with server(302, seen, location=location) as url:
        request = urllib.request.Request(url, headers={"Authorization": "Bearer test-key"})
        with pytest.raises(RedirectRefused) as raised:
            open_no_redirect(request, timeout=2)
    assert raised.value.code == 302
    assert str(raised.value) == "HTTP Error 302: redirect refused"
    assert raised.value.read() == b""
    assert not raised.value.headers and raised.value.geturl() == ""
    raised.value.close()
