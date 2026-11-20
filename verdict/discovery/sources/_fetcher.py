"""Injectable HTTP fetcher used by all source adapters.

All network access in ``verdict/discovery/sources/`` goes through a
``Fetcher`` callable so tests can run fully offline by injecting a
dict-backed stub.

The default implementation wraps ``verdict.http_safety.open_no_redirect``
plus a bounded chunk-read consistent with the S04 discovery bounds.

For offline test stubs, see :func:`~tests.test_discovery_sources.make_stub_fetcher`
in the test suite.
"""

from __future__ import annotations

from typing import Protocol
from urllib.request import Request


class Fetcher(Protocol):
    """Fetch *url* and return the raw response bytes."""

    def __call__(
        self,
        url: str,
        *,
        timeout: float = 20.0,
        max_bytes: int = 16 * 1024 * 1024,
        headers: dict[str, str] | None = None,
    ) -> bytes: ...


def default_fetcher(
    url: str,
    *,
    timeout: float = 20.0,
    max_bytes: int = 16 * 1024 * 1024,
    headers: dict[str, str] | None = None,
) -> bytes:
    """Fetch *url* using :func:`~verdict.http_safety.open_no_redirect`.

    Reads in 64 KiB chunks, raising ``OSError`` if ``max_bytes`` is
    exceeded before the response is fully consumed.
    """
    from verdict.http_safety import open_no_redirect

    req_headers = {"Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with open_no_redirect(req, timeout=timeout) as response:
        chunks: list[bytes] = []
        total = 0
        chunk_size = 65_536
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise OSError(f"response from {url!r} exceeded {max_bytes} byte limit")
            chunks.append(chunk)
        return b"".join(chunks)
