"""BOD-265: Prime visibility refresh is bounded, auditable, and non-authoritative."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.harness_prime import HarnessPrimeError, refresh_omniroute_visibility, sync_models
from verdict.prime_inventory import SIDECAR_NAME


def _home(tmp_path: Path, ids: list[str]) -> Path:
    home = tmp_path / "agent"
    home.mkdir()
    (home / "models.json").write_text(
        json.dumps({"providers": {"omniroute": {"models": [{"id": value} for value in ids]}}}),
        encoding="utf-8",
    )
    return home


def _now() -> datetime:
    return datetime(2026, 9, 27, tzinfo=timezone.utc)


def test_refresh_writes_only_concrete_live_models_and_auditable_snapshot(tmp_path: Path) -> None:
    home = _home(tmp_path, ["dead/old"])
    rows: list[dict[str, Any]] = [
        {"id": "cx/live", "owned_by": "cx"},
        {"id": "auto/best-coding"},
        {"id": "combo/route"},
        {"id": "router/x"},
        {"id": "virtual/x"},
        {"id": "cc/combo-owned", "owned_by": "combo"},
    ]
    status = refresh_omniroute_visibility(
        prime_home=home,
        source="http://gateway/v1/models",
        fetch_rows=lambda: rows,
        now=_now,
    )
    assert status.refreshed and status.fresh and status.count == 1
    assert status.excluded_ids == ("auto/best-coding", "cc/combo-owned", "combo/route", "router/x", "virtual/x")
    data = json.loads((home / "models.json").read_text())
    assert [row["id"] for row in data["providers"]["omniroute"]["models"]] == ["cx/live"]
    sidecar = json.loads((home / SIDECAR_NAME).read_text())
    success = sidecar["last_success"]
    assert success["source"] == "http://gateway/v1/models"
    assert success["count"] == 1 and success["route_ids"] == ["cx/live"]
    assert success["digest"].startswith("sha256:") and success["refreshed_at"]
    assert sidecar["last_failure"] is None


def test_sync_models_rejects_opaque_rows_even_when_mixed_with_live(tmp_path: Path) -> None:
    home = _home(tmp_path, ["cx/old"])
    result = sync_models([{"id": "cx/live"}, {"id": "auto/best"}], prime_home=home)
    assert result.total == 1 and result.added == ("cx/live",) and result.removed == ("cx/old",)
    assert [row["id"] for row in json.loads((home / "models.json").read_text())["providers"]["omniroute"]["models"]] == ["cx/live"]
    with pytest.raises(HarnessPrimeError, match="no concrete models"):
        sync_models([{"id": "auto/best"}], prime_home=home)


def test_refresh_failure_preserves_lkg_records_failure_and_never_fetches_when_fresh(tmp_path: Path) -> None:
    home = _home(tmp_path, [])
    calls = 0

    def first() -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        return [{"id": "cx/live"}]

    good = refresh_omniroute_visibility(prime_home=home, source="gateway", fetch_rows=first, now=_now)
    assert good.refreshed and calls == 1
    cached = refresh_omniroute_visibility(
        prime_home=home,
        source="gateway",
        fetch_rows=lambda: pytest.fail("fresh cache must not issue a GET"),
        now=lambda: _now() + timedelta(seconds=30),
    )
    assert not cached.refreshed and cached.fresh and cached.digest == good.digest
    failed = refresh_omniroute_visibility(
        prime_home=home,
        source="gateway",
        fetch_rows=lambda: (_ for _ in ()).throw(TimeoutError()),
        force=True,
        now=lambda: _now() + timedelta(seconds=31),
    )
    assert not failed.refreshed and not failed.fresh
    assert failed.snapshot == good.snapshot and failed.failure is not None
    assert failed.failure.error_type == "TimeoutError"
    assert [row["id"] for row in json.loads((home / "models.json").read_text())["providers"]["omniroute"]["models"]] == ["cx/live"]
    state = json.loads((home / SIDECAR_NAME).read_text())
    assert state["last_success"]["digest"] == good.digest
    assert state["last_failure"]["error_type"] == "TimeoutError"


def test_lkg_visibility_cannot_make_a_route_launchable(tmp_path: Path) -> None:
    from verdict.admission import AdmissionBypassError, RuntimeEvidence, admit

    home = _home(tmp_path, [])
    refresh_omniroute_visibility(
        prime_home=home, source="gateway", fetch_rows=lambda: [{"id": "cx/live", "owned_by": "cx"}], now=_now
    )
    admitted = admit(
        [{"id": "cx/live", "owned_by": "cx"}],
        [{"provider": "cx", "isActive": True}],
        RuntimeEvidence(),
        now=_now(),
    )
    assert "cx/live" in admitted and not admitted.launchable("cx/live")
    with pytest.raises(AdmissionBypassError):
        admitted.require_launchable("cx/live", surface="test")


def test_refresh_rejects_an_empty_concrete_live_inventory_and_keeps_existing_visibility(tmp_path: Path) -> None:
    home = _home(tmp_path, ["cx/old"])
    result = refresh_omniroute_visibility(
        prime_home=home, source="gateway", fetch_rows=lambda: [{"id": "auto/best"}], now=_now
    )
    assert result.snapshot is None and result.failure is not None
    assert [row["id"] for row in json.loads((home / "models.json").read_text())["providers"]["omniroute"]["models"]] == ["cx/old"]
