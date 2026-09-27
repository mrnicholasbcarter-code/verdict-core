"""BOD-265: Prime visibility refresh is bounded, auditable, and non-authoritative."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.harness_prime import HarnessPrimeError, refresh_omniroute_visibility, sync_models
from verdict.prime_inventory import SIDECAR_NAME, _read, refresh_prime_inventory, sidecar_path


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
        prime_home=home, source="http://gateway/v1/models", fetch_rows=lambda: rows, now=_now
    )
    assert status.refreshed and status.fresh and status.count == 1
    assert status.excluded_ids == (
        "auto/best-coding",
        "cc/combo-owned",
        "combo/route",
        "router/x",
        "virtual/x",
    )
    data = json.loads((home / "models.json").read_text())
    assert [row["id"] for row in data["providers"]["omniroute"]["models"]] == ["cx/live"]
    sidecar = json.loads((home / SIDECAR_NAME).read_text())
    success = sidecar["last_success"]
    assert success["source"] == "http://gateway/v1/models"
    assert success["count"] == 1 and success["route_ids"] == ["cx/live"]
    expected_digest_body = json.dumps(
        {"excluded_ids": list(status.excluded_ids), "route_ids": ["cx/live"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_digest = "sha256:" + hashlib.sha256(expected_digest_body.encode()).hexdigest()
    assert success["excluded_ids"] == [
        "auto/best-coding",
        "cc/combo-owned",
        "combo/route",
        "router/x",
        "virtual/x",
    ]
    assert success["digest"] == expected_digest
    assert success["refreshed_at"]
    assert sidecar["last_failure"] is None


def test_sync_models_rejects_opaque_rows_even_when_mixed_with_live(tmp_path: Path) -> None:
    home = _home(tmp_path, ["cx/old"])
    result = sync_models([{"id": "cx/live"}, {"id": "auto/best"}], prime_home=home)
    assert result.total == 1 and result.added == ("cx/live",) and result.removed == ("cx/old",)
    assert [
        row["id"]
        for row in json.loads((home / "models.json").read_text())["providers"]["omniroute"][
            "models"
        ]
    ] == ["cx/live"]
    with pytest.raises(HarnessPrimeError, match="no concrete models"):
        sync_models([{"id": "auto/best"}], prime_home=home)


def test_refresh_failure_preserves_lkg_records_failure_and_never_fetches_when_fresh(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, [])
    calls = 0

    def first() -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        return [{"id": "cx/live"}]

    good = refresh_omniroute_visibility(
        prime_home=home, source="gateway", fetch_rows=first, now=_now
    )
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
    assert [
        row["id"]
        for row in json.loads((home / "models.json").read_text())["providers"]["omniroute"][
            "models"
        ]
    ] == ["cx/live"]
    state = json.loads((home / SIDECAR_NAME).read_text())
    assert state["last_success"]["digest"] == good.digest
    assert state["last_failure"]["error_type"] == "TimeoutError"


def test_lkg_visibility_cannot_make_a_route_launchable(tmp_path: Path) -> None:
    from verdict.admission import AdmissionBypassError, RuntimeEvidence, admit

    home = _home(tmp_path, [])
    refresh_omniroute_visibility(
        prime_home=home,
        source="gateway",
        fetch_rows=lambda: [{"id": "cx/live", "owned_by": "cx"}],
        now=_now,
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


def test_inventory_layer_rejects_empty_concrete_rows_before_apply(tmp_path: Path) -> None:
    """The refresh boundary, not only sync_models(), must reject an empty inventory."""
    applied = False

    def apply_rows(_rows: list[dict[str, str]]) -> None:
        nonlocal applied
        applied = True

    result = refresh_prime_inventory(
        models_path=tmp_path / "models.json",
        source="gateway",
        fetch_rows=lambda: [{"id": "auto/best"}],
        apply_rows=apply_rows,
        now=_now,
    )
    assert not result.refreshed and not result.fresh
    assert result.failure is not None and result.failure.error_type == "ValueError"
    assert not applied


def test_refresh_rejects_an_empty_concrete_live_inventory_and_keeps_existing_visibility(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, ["cx/old"])
    result = refresh_omniroute_visibility(
        prime_home=home, source="gateway", fetch_rows=lambda: [{"id": "auto/best"}], now=_now
    )
    assert result.snapshot is None and result.failure is not None
    assert [
        row["id"]
        for row in json.loads((home / "models.json").read_text())["providers"]["omniroute"][
            "models"
        ]
    ] == ["cx/old"]


def test_refresh_bootstraps_only_a_minimal_visibility_provider(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    home.mkdir()
    refresh_omniroute_visibility(
        prime_home=home, source="gateway", fetch_rows=lambda: [{"id": "cx/live"}], now=_now
    )
    data = json.loads((home / "models.json").read_text())
    assert data == {
        "providers": {
            "omniroute": {
                "models": [
                    {
                        "id": "cx/live",
                        "name": "cx/live",
                        "reasoning": False,
                        "input": ["text"],
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    }
                ]
            }
        }
    }


def test_sidecar_digest_rejects_tampered_route_ids_and_excluded_ids(tmp_path: Path) -> None:
    home = _home(tmp_path, [])
    status = refresh_omniroute_visibility(
        prime_home=home,
        source="gateway",
        fetch_rows=lambda: [{"id": "cx/live"}, {"id": "auto/best"}],
        now=_now,
    )
    assert status.snapshot is not None
    path = sidecar_path(home / "models.json")
    payload = json.loads(path.read_text())
    payload["last_success"]["route_ids"] = ["cx/tampered"]
    path.write_text(json.dumps(payload))
    snapshot, _failure = _read(path)
    assert snapshot is None
    payload["last_success"]["route_ids"] = ["cx/live"]
    payload["last_success"]["excluded_ids"] = []
    path.write_text(json.dumps(payload))
    snapshot, _failure = _read(path)
    assert snapshot is None


def test_source_change_forces_refresh_not_cross_source_cache_hit(tmp_path: Path) -> None:
    home = _home(tmp_path, [])
    first = refresh_omniroute_visibility(
        prime_home=home, source="gateway-A", fetch_rows=lambda: [{"id": "cx/a"}], now=_now
    )
    calls = 0

    def second_rows() -> list[dict[str, str]]:
        nonlocal calls
        calls += 1
        return [{"id": "cx/b"}]

    second = refresh_omniroute_visibility(
        prime_home=home,
        source="gateway-B",
        fetch_rows=second_rows,
        now=lambda: _now() + timedelta(seconds=1),
    )
    assert first.fresh and second.refreshed and second.fresh and calls == 1
    assert second.source == "gateway-B"
    assert second.snapshot is not None and second.snapshot.route_ids == ("cx/b",)
