"""Revive is a queued controller request, never executed (spec v6.1 phase f, D2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.xfail(
    strict=True, raises=ImportError, reason="phase f: revive requests not implemented yet"
)
NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


def test_revive_emits_request_never_calls_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from verdict.omniroute_pipeline.revive import (
        queue_revive_request,  # type: ignore[import-not-found]
    )

    def boom(*a: object, **k: object) -> None:
        raise AssertionError("revive must never call an endpoint")

    for name in ("post", "put", "request", "get"):
        monkeypatch.setattr(httpx, name, boom)
    monkeypatch.setattr(httpx.Client, "request", boom)
    req = queue_revive_request(
        kind="test_all",
        provider="nvidia",
        model_id="nvidia/z-ai/glm-5.3",
        connection_id="c1",
        outbox=tmp_path / "outbox.jsonl",
        now=NOW,
    )
    assert req is not None
    assert req.executed is False
    assert req.payload["autoHideFailed"] is False
    assert req.payload["models"] == ["nvidia/z-ai/glm-5.3"]
    assert (tmp_path / "outbox.jsonl").read_text().count("\n") == 1


def test_revive_rate_limit_once_per_provider_24h(tmp_path: Path) -> None:
    from verdict.omniroute_pipeline.revive import (
        queue_revive_request,  # type: ignore[import-not-found]
    )

    out = tmp_path / "outbox.jsonl"
    assert queue_revive_request(kind="sync_models", provider="nvidia", outbox=out, now=NOW)
    assert (
        queue_revive_request(
            kind="sync_models", provider="nvidia", outbox=out, now=NOW + timedelta(hours=23)
        )
        is None
    )
    assert queue_revive_request(
        kind="sync_models", provider="nvidia", outbox=out, now=NOW + timedelta(hours=25)
    )


def test_irreversible_ops_never_requested_automatically(tmp_path: Path) -> None:
    from verdict.omniroute_pipeline.revive import (
        queue_revive_request,  # type: ignore[import-not-found]
    )

    for kind in ("delete_connection", "rotate_key", "hide_model", "test_batch"):
        with pytest.raises(ValueError):
            queue_revive_request(kind=kind, provider="nvidia", outbox=tmp_path / "o.jsonl", now=NOW)
