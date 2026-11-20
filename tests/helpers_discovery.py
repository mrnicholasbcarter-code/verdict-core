"""Offline test helpers for verdict.discovery.sources tests.

:func:`make_stub_fetcher` returns a :class:`~verdict.discovery.sources._fetcher.Fetcher`
backed by a plain dict of ``{url: response}`` pairs, so discovery adapter tests
run with no network I/O.
"""

from __future__ import annotations

import json
from collections.abc import Callable


def make_stub_fetcher(
    responses: dict[str, bytes | str | dict[str, object] | list[object]],
) -> Callable[..., bytes]:
    """Return a Fetcher that serves pre-built responses from *responses*.

    Values may be ``bytes`` (returned as-is), ``str`` (UTF-8 encoded) or
    any JSON-serialisable object (JSON-encoded).  Missing URLs raise
    ``KeyError`` so tests fail fast on unexpected fetches.
    """

    def _stub(
        url: str,
        *,
        timeout: float = 20.0,
        max_bytes: int = 16 * 1024 * 1024,
        headers: dict[str, str] | None = None,
    ) -> bytes:
        raw = responses[url]
        if isinstance(raw, bytes):
            return raw
        if isinstance(raw, str):
            return raw.encode()
        return json.dumps(raw).encode()

    return _stub
