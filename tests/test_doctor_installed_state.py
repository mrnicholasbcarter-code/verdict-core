"""Doctor's installed-state rows: timers and health-cache freshness (BOD-411 AC1/AC2).

``installed_service_rows``/``health_cache_row`` are exercised directly with a
fake systemd runner and an injectable clock, instead of only patching
unrelated doctor tests so they keep passing.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict import doctor_diagnostics as dd
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult

NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)


def _fake_run(stdout: str):
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

    return run


@pytest.fixture(autouse=True)
def _isolated_health_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache_path = tmp_path / "health-cache.json"
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(cache_path))
    return cache_path


def _record_one_route(cache_path: Path, *, checked_at: datetime) -> None:
    cache = HealthCache(cache_path)
    cache.record(
        "test/route",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True, http_status=200),
        checked_at,
    )
    cache.save()


# ---------------------------------------------------------------------------
# health_cache_row
# ---------------------------------------------------------------------------


def test_health_cache_row_fails_when_file_missing() -> None:
    row = dd.health_cache_row(now=NOW)
    assert row["status"] == "failed"
    assert "does not exist" in row["detail"]


def test_health_cache_row_accepts_usable_evidence_without_legacy_probe_cadence(
    _isolated_health_cache: Path,
) -> None:
    # Usable evidence is not expired merely because the retired 300s loop is off.
    _record_one_route(_isolated_health_cache, checked_at=NOW - timedelta(seconds=601))

    row = dd.health_cache_row(now=NOW)

    assert row["status"] == "ok"
    assert "fresh" in row["detail"]


def test_health_cache_row_ok_when_newest_entry_within_2x_probe_interval(
    _isolated_health_cache: Path,
) -> None:
    _record_one_route(_isolated_health_cache, checked_at=NOW - timedelta(seconds=599))

    row = dd.health_cache_row(now=NOW)

    assert row["status"] == "ok"


def test_health_cache_row_fails_after_evidence_lifetime_and_grace(
    _isolated_health_cache: Path,
) -> None:
    from verdict.orchestration.health_cache import USABLE_SECONDS

    _record_one_route(
        _isolated_health_cache, checked_at=NOW - timedelta(seconds=USABLE_SECONDS + 1)
    )
    row = dd.health_cache_row(now=NOW)
    assert row["status"] == "failed"
    assert "no fresh or stale route" in row["detail"]


def test_health_cache_row_fails_when_empty(_isolated_health_cache: Path) -> None:
    HealthCache(_isolated_health_cache).save()
    row = dd.health_cache_row(now=NOW)
    assert row["status"] == "failed"
    assert "no fresh or stale route" in row["detail"]


def test_health_cache_row_fails_when_fresh_plus_stale_is_zero(_isolated_health_cache: Path) -> None:
    # A negative-category entry is neither fresh nor stale: fresh + stale == 0.
    cache = HealthCache(_isolated_health_cache)
    cache.record(
        "test/route",
        ProbeResult(category="rate_limited", chat_ok=False, tool_ok=False, http_status=429),
        NOW,
    )
    cache.save()

    row = dd.health_cache_row(now=NOW)

    assert row["status"] == "failed"
    assert "no fresh or stale route" in row["detail"]


# ---------------------------------------------------------------------------
# installed_service_rows
# ---------------------------------------------------------------------------


def test_installed_service_rows_fails_when_identify_timer_missing(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.identify_service import TIMER as IDENTIFY_TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)

    def fake_timer_state(unit: str) -> dict[str, object]:
        if unit == IDENTIFY_TIMER:
            return {"installed": False, "failed": False, "active": False, "state": "missing"}
        return {"installed": True, "failed": False, "active": True, "state": "active"}

    monkeypatch.setattr(dd, "timer_state", fake_timer_state, raising=False)
    import verdict.prove_at_rest_service as pars

    monkeypatch.setattr(pars, "timer_state", fake_timer_state)

    rows = dd.installed_service_rows(now=NOW, timer_state=fake_timer_state)

    prove_row = next(r for r in rows if r["label"] == "Identify timer")
    assert prove_row["status"] == "failed"
    assert IDENTIFY_TIMER in prove_row["detail"]


def test_installed_service_rows_reports_metadata_timer_never_completed(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    _record_one_route(_isolated_health_cache, checked_at=NOW)

    from verdict.metadata_service import TIMER as METADATA_TIMER

    states = {
        METADATA_TIMER: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": None,
        }
    }

    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: states.get(
            unit, {"installed": True, "failed": False, "active": True, "state": "active"}
        ),
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert "never completed" in metadata_row["detail"]


def test_installed_service_rows_fails_when_prime_sync_timer_is_failed(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_one_route(_isolated_health_cache, checked_at=NOW)

    def fake_timer_state(unit: str) -> dict[str, object]:
        if unit == "verdict-prime-sync.timer":
            return {"installed": True, "failed": True, "active": False, "state": "failed"}
        return {"installed": True, "failed": False, "active": True, "state": "active"}

    import verdict.prove_at_rest_service as pars

    monkeypatch.setattr(pars, "timer_state", fake_timer_state)

    rows = dd.installed_service_rows(now=NOW)

    sync_row = next(r for r in rows if r["label"] == "Prime-sync timer")
    assert sync_row["status"] == "failed"
    assert "verdict-prime-sync.timer" in sync_row["detail"]


def test_installed_service_rows_all_ok_when_timers_active_and_cache_fresh(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    source_names = ("models.dev", "models.dev.models", "litellm")
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=NOW.isoformat().replace("+00:00", "Z"),
            sources={
                source: SourceStatus(
                    source=source,
                    status="ok",
                    fetched_at=NOW.isoformat().replace("+00:00", "Z"),
                    record_count=1,
                )
                for source in source_names
            },
            records=(),
        )
    )

    import verdict.metadata_service as metadata_service
    import verdict.prove_at_rest_service as pars

    def fake_timer(unit: str) -> dict[str, object]:
        del unit
        return {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC",
            "last_completion": "Fri 2026-10-16 11:56:00 UTC",
            "result": "success",
        }

    monkeypatch.setattr(pars, "timer_state", fake_timer)
    monkeypatch.setattr(metadata_service, "timer_state", fake_timer)
    import json

    from verdict.identify_service import latest_report_path

    latest_report_path().write_text(
        json.dumps(
            {
                "mode": "passive",
                "model_calls": 0,
                "source_errors": [],
                "fetched_at": NOW.isoformat(),
            }
        )
    )

    rows = dd.installed_service_rows(now=NOW, timer_state=fake_timer)

    assert all(r["status"] == "ok" for r in rows)


def test_metadata_timer_absent_is_an_explicit_opt_in_warning(
    _isolated_health_cache: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))

    def timer(unit: str) -> dict[str, object]:
        if unit == TIMER:
            return {"installed": False, "state": "missing", "active": False, "failed": False}
        return {"installed": True, "state": "active", "active": True, "failed": False}

    rows = dd.installed_service_rows(now=NOW, timer_state=timer)
    row = next(item for item in rows if item["label"] == "Metadata refresh timer")
    assert row["status"] == "warning"
    assert "not installed" in row["detail"]
    assert "explicit opt-in" in row["detail"]
    assert not (tmp_path / ".verdict").exists()


def test_metadata_timer_doctor_failure_recovers_after_successful_refresh(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    fetched_at = NOW.isoformat().replace("+00:00", "Z")
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=fetched_at,
            sources={
                source: SourceStatus(
                    source=source, status="ok", fetched_at=fetched_at, record_count=1
                )
                for source in ("models.dev", "models.dev.models", "litellm")
            },
            records=(),
        )
    )
    states = {
        "metadata": {
            "installed": True,
            "failed": True,
            "active": False,
            "state": "failed",
            "last_trigger": None,
            "result": "exit-code",
        }
    }

    def timer(unit: str) -> dict[str, object]:
        if unit == TIMER:
            return states["metadata"]
        return {"installed": True, "failed": False, "active": True, "state": "active"}

    failed = dd.installed_service_rows(now=NOW, timer_state=timer)
    failed_row = next(row for row in failed if row["label"] == "Metadata refresh timer")
    assert failed_row["status"] == "failed"
    assert "never completed" in failed_row["detail"]

    states["metadata"] = {
        "installed": True,
        "failed": False,
        "active": True,
        "state": "active",
        "last_trigger": "Fri 2026-10-16 11:55:00 UTC",
        "result": "success",
    }
    started = dd.installed_service_rows(now=NOW, timer_state=timer)
    started_row = next(row for row in started if row["label"] == "Metadata refresh timer")
    assert started_row["status"] == "failed"
    assert "never completed" in started_row["detail"]

    states["metadata"]["last_completion"] = "Fri 2026-10-16 11:56:00 UTC"
    recovered = dd.installed_service_rows(now=NOW, timer_state=timer)
    recovered_row = next(row for row in recovered if row["label"] == "Metadata refresh timer")
    assert recovered_row["status"] == "ok"
    assert "models.dev=0s" in recovered_row["detail"]


def test_metadata_doctor_reports_stale_source_age(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    fetched_at = (NOW - timedelta(hours=13)).isoformat().replace("+00:00", "Z")
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=fetched_at,
            sources={
                source: SourceStatus(
                    source=source, status="ok", fetched_at=fetched_at, record_count=1
                )
                for source in ("models.dev", "models.dev.models", "litellm")
            },
            records=(),
        )
    )
    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:00:00 UTC" if unit == TIMER else None,
            "result": "success",
        },
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert "older than 12 hours" in metadata_row["detail"]


@pytest.mark.parametrize(
    ("fetched_at", "expected_detail"),
    [
        ("2026-10-16T12:00:00", "unreadable fetch time"),
        ("not-a-time", "unreadable fetch time"),
        ("2026-10-16T13:00:00Z", "fetch time is in the future"),
        (42, "unreadable fetch time"),  # type: ignore[arg-type]
    ],
)
def test_metadata_doctor_fails_closed_on_invalid_source_timestamps(
    _isolated_health_cache: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fetched_at: str,
    expected_detail: str,
) -> None:
    from verdict.metadata import METADATA_SCHEMA_VERSION, SourceStatus, save_store
    from verdict.metadata.store import MetadataSnapshot
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    sources = {
        source: SourceStatus(
            source=source,
            status="ok",
            fetched_at=NOW.isoformat().replace("+00:00", "Z"),
            record_count=1,
        )
        for source in ("models.dev", "models.dev.models", "litellm")
    }
    sources["models.dev"] = SourceStatus(
        source="models.dev", status="ok", fetched_at=fetched_at, record_count=1
    )
    save_store(
        MetadataSnapshot(
            schema_version=METADATA_SCHEMA_VERSION,
            refreshed_at=NOW.isoformat().replace("+00:00", "Z"),
            sources=sources,
            records=(),
        )
    )
    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC" if unit == TIMER else None,
            "result": "success",
        },
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert expected_detail in metadata_row["detail"]


def test_metadata_doctor_keeps_missing_service_result_explicit(
    _isolated_health_cache: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from verdict.metadata_service import TIMER

    _record_one_route(_isolated_health_cache, checked_at=NOW)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    rows = dd.installed_service_rows(
        now=NOW,
        timer_state=lambda unit: {
            "installed": True,
            "failed": False,
            "active": True,
            "state": "active",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC" if unit == TIMER else None,
        },
    )

    metadata_row = next(row for row in rows if row["label"] == "Metadata refresh timer")
    assert metadata_row["status"] == "failed"
    assert "last service result is unknown" in metadata_row["detail"]


# ---------------------------------------------------------------------------
# installed runtime provenance (BOD-414 / S11, acceptance A29)
# ---------------------------------------------------------------------------


def _fake_site_packages(root: Path, commit: str) -> Path:
    """A uv-tool style install: package plus dist-info recording the git commit."""
    package = root / "site-packages" / "verdict"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.5.0"\n')
    dist = root / "site-packages" / "verdict_core-0.5.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: verdict-core\nVersion: 0.5.0\n")
    (dist / "direct_url.json").write_text(
        '{"url": "https://example.invalid/verdict-core", "vcs_info": {"vcs": "git",'
        f' "commit_id": "{commit}"}}}}'
    )
    return package


def _fake_checkout(root: Path, commit: str) -> Path:
    """A source checkout whose HEAD is ``commit`` (detached), like a stale worktree."""
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text(commit + "\n")
    package = root / "verdict"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.5.0"\n')
    return package


def _unit(cwd: Path, interpreter: str, argv: str, environment: str = "") -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "WorkingDirectory": str(cwd),
        "ExecStart": f"{{ path={interpreter} ; argv[]={interpreter} {argv} ; status=0/0 }}",
        "Environment": environment,
    }


def test_installed_runtime_provenance_and_shared_coordinator_cutover(tmp_path: Path) -> None:
    reviewed_commit = "4f642962894da7ac4c84615eac3bcc4b35f6d1f3"
    reviewed = _fake_site_packages(tmp_path / "uv-tool", reviewed_commit)
    tool_python = str(tmp_path / "uv-tool" / "bin" / "python")
    # Copy 1: the operator's source checkout (prime-sync cwd + its .venv), at another commit.
    source = _fake_checkout(
        tmp_path / "dev" / "verdict-core", "31873e6045ce04a47f98000927c28c82270da96a"
    )
    source_python = str(tmp_path / "dev" / "verdict-core" / ".venv" / "bin" / "python")
    # Copy 2: a stranded worktree with its own venv (the acceptance job today).
    stale = _fake_checkout(
        tmp_path / "worktrees" / "train2", "5a941b68aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    stale_python = str(tmp_path / "worktrees" / "train2" / ".venv" / "bin" / "python")
    # Copy 3: the census runtime copy (package only, no recorded build).
    runtime = tmp_path / "census-runtime" / "repo"
    (runtime / "verdict").mkdir(parents=True)
    (runtime / "verdict" / "__init__.py").write_text('__version__ = "0.5.0"\n')

    units = {
        "verdict-prime-sync.service": _unit(
            source.parent, source_python, "-m verdict harness prime sync-models"
        ),
        "verdict-acceptance.service": _unit(
            stale.parent, stale_python, "-m verdict acceptance run --notify dry-run"
        ),
        "verdict-identify.service": _unit(
            runtime, source_python, "-m verdict census --max-per-provider 2"
        ),
        "verdict-identify-screen.service": _unit(
            tmp_path,
            tool_python,
            "-m verdict prove-at-rest daemon --allow-live-probe --interval 300",
            environment=f"VERDICT_HOME={tmp_path / 'other-home'} OMNIROUTE_BASE_URL=http://gw:20128",
        ),
        "verdict-metadata-refresh.service": _unit(
            reviewed.parent, tool_python, "-m verdict metadata refresh --include-p1"
        ),
    }
    # What each interpreter imports from each cwd: cwd shadows, else its own install.
    installs = {source_python: source, stale_python: stale, tool_python: reviewed}

    def resolve(interpreter: str, cwd: str) -> str | None:
        if (Path(cwd) / "verdict" / "__init__.py").is_file():
            return str(Path(cwd) / "verdict")
        return str(installs[interpreter])

    rows = dd.installed_runtime_rows(
        unit_props=lambda unit: units.get(unit, {"LoadState": "not-found"}),
        resolve=resolve,
        reviewed_package=reviewed,
        home=tmp_path / "home",
        state_home=tmp_path / "home" / ".verdict",
    )
    by_unit = {row["unit"]: row for row in rows if "unit" in row}

    # All three source-copy mismatches are named with the package they import.
    prime = by_unit["verdict-prime-sync.service"]
    assert prime["status"] == "failed"
    assert str(source) in prime["detail"] and "31873e6045ce" in prime["detail"]
    acceptance_row = by_unit["verdict-acceptance.service"]
    assert acceptance_row["status"] == "failed"
    assert str(stale) in acceptance_row["detail"] and "5a941b68aaaa" in acceptance_row["detail"]
    census = by_unit["verdict-identify.service"]
    assert census["status"] == "failed"
    assert str(runtime / "verdict") in census["detail"]
    assert "unrecorded" in census["detail"]
    # Reviewed build, but a different state scope cannot count as main proof.
    prove = by_unit["verdict-identify-screen.service"]
    assert prove["status"] == "failed"
    assert "state scope" in prove["detail"] and str(tmp_path / "other-home") in prove["detail"]
    assert "http://gw:20128" in prove["detail"]
    # Matching build and config passes and still shows build, imports, gateway and scope.
    metadata_row = by_unit["verdict-metadata-refresh.service"]
    assert metadata_row["status"] == "ok", metadata_row
    assert str(reviewed) in metadata_row["detail"]
    assert reviewed_commit[:12] in metadata_row["detail"]
    assert "gateway default" in metadata_row["detail"]
    assert str(tmp_path / "home" / ".verdict") in metadata_row["detail"]

    assert not any(row["label"] == "Qualification owner" for row in rows)

    # Converged host: every unit on the reviewed build and scope. The census
    # runtime copy counts only when it records the reviewed build and content.
    converged = {
        name: _unit(
            reviewed.parent, tool_python, unit["ExecStart"].split(" ; ")[1].split(" ", 1)[1]
        )
        for name, unit in units.items()
    }
    converged["verdict-identify.service"] = _unit(
        runtime, tool_python, "-m verdict census --max-per-provider 2"
    )
    (runtime / "verdict" / "__init__.py").write_text('__version__ = "0.5.0"\n')
    (runtime / "verdict-build.json").write_text(
        f'{{"build": "git {reviewed_commit[:12]}", "source": "{reviewed}"}}'
    )
    rows = dd.installed_runtime_rows(
        unit_props=lambda unit: converged.get(unit, {"LoadState": "not-found"}),
        resolve=resolve,
        reviewed_package=reviewed,
        home=tmp_path / "home",
        state_home=tmp_path / "home" / ".verdict",
    )
    assert [row["status"] for row in rows] == ["ok"] * len(rows), rows
    assert not any(row["label"] == "Qualification owner" for row in rows)

    # A separate health cache is a separate coordinator lock and ledger.
    converged["verdict-identify.service"]["Environment"] = (
        f"VERDICT_HEALTH_CACHE={tmp_path / 'elsewhere' / 'health-cache.json'}"
    )
    rows = dd.installed_runtime_rows(
        unit_props=lambda unit: converged.get(unit, {"LoadState": "not-found"}),
        resolve=resolve,
        reviewed_package=reviewed,
        home=tmp_path / "home",
        state_home=tmp_path / "home" / ".verdict",
    )
    census = next(row for row in rows if row.get("unit") == "verdict-identify.service")
    assert census["status"] == "failed" and "health cache" in census["detail"]
    assert not any(row["label"] == "Qualification owner" for row in rows)
    converged["verdict-identify.service"]["Environment"] = ""

    # A copy whose content drifted from the reviewed build fails even with a stamp.
    (runtime / "verdict" / "extra.py").write_text("STALE = True\n")
    rows = dd.installed_runtime_rows(
        unit_props=lambda unit: converged.get(unit, {"LoadState": "not-found"}),
        resolve=resolve,
        reviewed_package=reviewed,
        home=tmp_path / "home",
        state_home=tmp_path / "home" / ".verdict",
    )
    census = next(row for row in rows if row.get("unit") == "verdict-identify.service")
    assert census["status"] == "failed"
    assert "content differs" in census["detail"]

    # An interpreter whose own install is another copy fails even from a clean cwd.
    converged["verdict-identify.service"] = _unit(
        reviewed.parent, source_python, "-m verdict census --max-per-provider 2"
    )
    rows = dd.installed_runtime_rows(
        unit_props=lambda unit: converged.get(unit, {"LoadState": "not-found"}),
        resolve=resolve,
        reviewed_package=reviewed,
        home=tmp_path / "home",
        state_home=tmp_path / "home" / ".verdict",
    )
    census = next(row for row in rows if row.get("unit") == "verdict-identify.service")
    assert census["status"] == "failed"
    assert f"interpreter {source_python} installs {source}" in census["detail"]


def test_installed_runtime_unrecorded_build_is_never_ok_and_gateway_is_redacted(
    tmp_path: Path,
) -> None:
    """Review fixes on 6bf2335f: a unit importing the reviewed path itself is OK
    only with a recorded build, and a gateway URL never leaks userinfo or query."""
    checkout = tmp_path / "dev" / "verdict-core"
    package = checkout / "verdict"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "0.5.0"\n')
    python = str(checkout / ".venv" / "bin" / "python")
    units = {
        "verdict-prime-sync.service": _unit(
            checkout,
            python,
            "-m verdict harness prime sync-models",
            environment="OMNIROUTE_BASE_URL=http://user:secret@gw:20128/tok-secret/v1?key=secret#frag",
        )
    }

    def rows() -> list[dict[str, str]]:
        return dd.installed_runtime_rows(
            unit_props=lambda unit: units.get(unit, {"LoadState": "not-found"}),
            resolve=lambda interpreter, cwd: str(package),
            reviewed_package=package,
            home=tmp_path / "home",
            state_home=tmp_path / "home" / ".verdict",
        )

    # Same path as the reviewed package, but no recorded build: unknown, not OK.
    row = next(r for r in rows() if r.get("unit") == "verdict-prime-sync.service")
    assert row["status"] == "failed", row
    assert "unrecorded" in row["detail"]
    assert "secret" not in row["detail"] and "user" not in row["detail"]
    assert "gateway http://gw:20128/..., " in row["detail"]

    # With a recorded build the same unit is OK, and the URL stays redacted.
    (checkout / ".git").mkdir()
    (checkout / ".git" / "HEAD").write_text("4f642962894da7ac4c84615eac3bcc4b35f6d1f3\n")
    row = next(r for r in rows() if r.get("unit") == "verdict-prime-sync.service")
    assert row["status"] == "ok", row
    assert "git 4f642962894d" in row["detail"]
    assert "secret" not in row["detail"]


@pytest.mark.parametrize("unit", ["verdict-census.service", "verdict-prove-at-rest.service"])
def test_retired_disabled_runtime_is_note_without_import_probe(tmp_path, unit):
    props = {
        unit: {"LoadState": "loaded", "WorkingDirectory": "/stale/repo"},
        unit.replace(".service", ".timer"): {
            "LoadState": "loaded",
            "UnitFileState": "disabled",
            "ActiveState": "inactive",
        },
    }
    rows = dd.installed_runtime_rows(
        unit_props=lambda name: props.get(name, {"LoadState": "not-found"}),
        resolve=lambda *_: pytest.fail("retired runtime must not be imported"),
        reviewed_package=tmp_path,
    )
    row = next(row for row in rows if row.get("unit") == unit)
    assert row["status"] == "note"
    assert "retired; disabled" in row["detail"]
    assert not any(row["label"] == "Qualification owner" for row in rows)


@pytest.mark.parametrize("unit", ["verdict-census.service", "verdict-prove-at-rest.service"])
@pytest.mark.parametrize("enabled,active", [("enabled", "inactive"), ("disabled", "active")])
def test_retired_timer_reactivation_fails_even_on_reviewed_build(tmp_path, unit, enabled, active):
    props = {
        unit: {"LoadState": "loaded"},
        unit.replace(".service", ".timer"): {
            "LoadState": "loaded",
            "UnitFileState": enabled,
            "ActiveState": active,
        },
    }
    rows = dd.installed_runtime_rows(
        unit_props=lambda name: props.get(name, {"LoadState": "not-found"}),
        resolve=lambda *_: str(tmp_path),
        reviewed_package=tmp_path,
    )
    row = next(row for row in rows if row.get("unit") == unit)
    assert row["status"] == "failed"
    assert "retired timer" in row["detail"]
    owner = next(row for row in rows if row["label"] == "Qualification owner")
    assert owner["status"] == "failed"
