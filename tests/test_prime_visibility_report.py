"""BOD-265: read-only Prime visibility drift report."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.orchestration.eligibility_report import prime_visibility_report


def _home(tmp_path: Path, visible: list[str]) -> Path:
    home = tmp_path / "agent"
    home.mkdir()
    data = {"providers": {"omniroute": {"models": [{"id": rid} for rid in visible]}}}
    (home / "models.json").write_text(json.dumps(data))
    return home


def test_report_shows_counts_and_drift_in_both_directions(tmp_path: Path) -> None:
    home = _home(tmp_path, ["kr/a", "kr/dead"])
    before = (home / "models.json").read_bytes()
    live = [{"id": "kr/a"}, {"id": "kr/new"}, {"id": "auto/best-coding"}]
    report = prime_visibility_report(live, prime_home=home)
    assert report["live_count"] == 2
    assert report["live_opaque_excluded"] == 1
    assert report["prime_visible_count"] == 2
    assert report["live_not_visible"] == ["kr/new"]
    assert report["visible_not_live"] == ["kr/dead"]
    assert report["in_sync"] is False
    assert report["last_refresh"] is None
    # Read-only: the registry is never written.
    assert (home / "models.json").read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["models.json"]


def test_report_in_sync_and_includes_last_refresh_digest(tmp_path: Path) -> None:
    from verdict.harness_prime import refresh_omniroute_visibility

    home = _home(tmp_path, ["kr/a"])
    rows = [{"id": "kr/a", "owned_by": "kiro"}]
    refresh_omniroute_visibility(
        prime_home=home, source="omniroute:/v1/models", fetch_rows=lambda: rows, force=True
    )
    report = prime_visibility_report(rows, prime_home=home)
    assert report["in_sync"] is True
    assert report["last_refresh"]["count"] == 1
    assert str(report["last_refresh"]["digest"]).startswith("sha256:")
    assert report["last_refresh"]["route_ids"] is None  # counts only, no id dump


def test_unreadable_registry_is_reported_not_raised(tmp_path: Path) -> None:
    home = tmp_path / "agent"
    home.mkdir()
    report = prime_visibility_report([{"id": "kr/a"}], prime_home=home)
    assert report["registry_error"] == "FileNotFoundError"
    assert report["in_sync"] is False


def test_text_output_lists_every_drift_id(tmp_path: Path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """Never truncated: every missing/extra id is printed."""
    import argparse

    from verdict.orchestration import cli as orch_cli
    from verdict.orchestration import run as orch_run

    home = _home(tmp_path, [f"kr/old-{i:03d}" for i in range(40)])
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.setattr(
        orch_cli,
        "prime_visibility_report",
        lambda rows: prime_visibility_report(rows, prime_home=home),
    )
    live = [{"id": f"kr/new-{i:03d}"} for i in range(40)]
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gateway, api_key: live)
    code = orch_cli._prime_visibility(argparse.Namespace(gateway="http://127.0.0.1:1", json=False))
    out = capsys.readouterr().out
    assert code == 1 and "status: DRIFT" in out
    assert all(f"+ kr/new-{i:03d}" in out for i in range(40))
    assert all(f"- kr/old-{i:03d}" in out for i in range(40))
