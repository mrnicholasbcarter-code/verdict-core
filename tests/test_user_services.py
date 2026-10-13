"""Verdict user services: key resolution, unit install, and doctor/setup checks."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict import user_services
from verdict.credentials_store import resolve_api_key, resolve_gateway_key
from verdict.orchestration.health_cache import HealthCache, ProbeResult

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def _write_secret(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    return tmp_path


# -- one key resolver ---------------------------------------------------------


def test_key_resolver_precedence(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert resolve_gateway_key() is None
    _write_secret(home / ".prime/agent/auth.json", json.dumps({"omniroute": {"key": "prime"}}))
    assert resolve_gateway_key() == ("prime", "prime_auth")
    _write_secret(home / ".config/verdict/omniroute-api-key", "file\n")
    assert resolve_gateway_key() == ("file", "key_file")
    _write_secret(home / ".config/verdict/credentials.env", "OMNIROUTE_API_KEY=store\n")
    assert resolve_gateway_key() == ("store", "credential_store:OMNIROUTE_API_KEY")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "env")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "verdict-env")
    assert resolve_api_key() == "verdict-env"
    # What a systemd user unit sees: no exported key.
    assert resolve_gateway_key(environ={}) == ("store", "credential_store:OMNIROUTE_API_KEY")


def test_key_resolver_skips_files_others_can_read(home: Path) -> None:
    _write_secret(home / ".config/verdict/omniroute-api-key", "loose", mode=0o644)
    _write_secret(home / ".prime/agent/auth.json", "{not json", mode=0o600)
    assert resolve_gateway_key() is None


def test_prime_sync_uses_key_file_when_env_is_unset(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 401 root cause: the unit has no exported key, so the file must be read."""
    from verdict.orchestration import cli as orch_cli

    _write_secret(home / ".config/verdict/omniroute-api-key", "file-key")
    seen: list[str | None] = []

    def fake_inventory(gateway: str, *, api_key: str | None, timeout: float = 30) -> list[dict]:
        seen.append(api_key)
        raise OSError("stop after auth")

    monkeypatch.setattr("verdict.orchestration.run.fetch_inventory", fake_inventory)
    args = type("A", (), {"gateway": "http://127.0.0.1:1", "dry_run": True})()
    assert orch_cli._prime_sync_models(args) == 2
    assert seen == ["file-key"]


# -- units --------------------------------------------------------------------


class FakeSystemd:
    """Records commands; answers ``show`` from a mutable state table."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.state: dict[str, dict[str, str]] = {}
        self.journal = "BLOCKED: HTTP Error 401: Unauthorized"

    def set(self, name: str, **props: str) -> None:
        self.state[name] = {"Id": name, "LoadState": "loaded", **props}

    def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(args))
        if args[0] == "journalctl":
            return 0, f"starting\n{self.journal}"
        if args[2] == "show":
            names = [a for a in args[3:] if a.endswith((".service", ".timer"))]
            blocks = [
                "\n".join(
                    f"{k}={v}"
                    for k, v in self.state.get(n, {"Id": n, "LoadState": "not-found"}).items()
                )
                for n in names
            ]
            return 0, "\n\n".join(blocks)
        return 0, ""


def _healthy(fake: FakeSystemd, unit: user_services.UserUnit) -> None:
    fake.set(unit.timer, ActiveState="active", SubState="waiting")
    fake.set(unit.service, ActiveState="inactive", SubState="dead", Result="success")


def test_install_refuses_without_a_key_a_unit_can_read(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "shell-only")
    fake = FakeSystemd()
    with pytest.raises(user_services.ServiceError, match="omniroute-api-key"):
        user_services.manage_units([user_services.prover_unit()], unit_dir=home / "u", run=fake)
    assert not (home / "u").exists() and not fake.calls


def test_install_writes_no_secret_and_is_idempotent(home: Path) -> None:
    _write_secret(home / ".config/verdict/omniroute-api-key", "secret-value-123")
    budget = user_services.ProberBudget(
        interval=90, agentic_max_routes=20, agentic_wall_seconds=300
    )
    units = [user_services.prover_unit(budget), user_services.prime_sync_unit()]
    fake = FakeSystemd()
    dry = user_services.manage_units(units, unit_dir=home / "u", dry_run=True, run=fake)
    assert not (home / "u").exists() and not fake.calls and dry["key_source"] == "key_file"
    first = user_services.manage_units(units, unit_dir=home / "u", run=fake)
    texts = {p.name: p.read_text() for p in (home / "u").iterdir()}
    assert sorted(texts) == sorted(first["files"])
    assert all("secret-value-123" not in t and "API_KEY" not in t for t in texts.values())
    prover = texts["verdict-prove-at-rest.service"]
    for flag in ("--interval 90", "--agentic-max-routes 20", "--agentic-wall-seconds 300"):
        assert flag in prover
    assert "harness prime sync-models" in texts["verdict-prime-sync.service"]
    assert ["systemctl", "--user", "start", "verdict-prime-sync.service"] in fake.calls
    assert ["systemctl", "--user", "restart", "verdict-prove-at-rest.service"] in fake.calls
    fake.calls.clear()
    second = user_services.manage_units(units, unit_dir=home / "u", run=fake)
    assert not second["changed"]
    assert all(call[2] == "enable" for call in fake.calls)
    user_services.manage_units(units, unit_dir=home / "u", uninstall=True, run=fake)
    assert not list((home / "u").iterdir())


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_rendered_units_pass_systemd_verify(tmp_path: Path) -> None:
    """The fake runner cannot see bad unit settings; systemd's own parser can."""
    import subprocess

    for unit in (user_services.prover_unit(), user_services.prime_sync_unit()):
        for name, body in unit.files().items():
            (tmp_path / name).write_text(body)
    done = subprocess.run(
        ["systemd-analyze", "verify", *sorted(str(p) for p in tmp_path.glob("*.service"))],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "bad unit file setting" not in done.stderr and "not absolute" not in done.stderr, (
        done.stderr
    )
    assert done.returncode == 0, done.stderr


def test_install_reports_a_failed_systemctl(home: Path) -> None:
    _write_secret(home / ".config/verdict/omniroute-api-key", "k")

    def broken(args: Sequence[str]) -> tuple[int, str]:
        return (1, "Failed to connect to bus") if args[2] == "daemon-reload" else (0, "")

    with pytest.raises(user_services.ServiceError, match="Failed to connect to bus"):
        user_services.manage_units([user_services.prover_unit()], unit_dir=home / "u", run=broken)


@pytest.mark.parametrize(
    "options", [{"interval": 0}, {"max_requests": 0}, {"interval": float("nan")}]
)
def test_prober_budget_rejects_unbounded_values(options: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        user_services.ProberBudget(**options)  # type: ignore[arg-type]


def test_prime_sync_unit_only_when_prime_is_installed(home: Path) -> None:
    assert [u.name for u in user_services.verdict_units()] == ["verdict-prove-at-rest"]
    _write_secret(home / ".prime/agent/models.json", "{}")
    assert [u.name for u in user_services.verdict_units()] == [
        "verdict-prove-at-rest",
        "verdict-prime-sync",
    ]


# -- checks -------------------------------------------------------------------


def test_check_units_names_missing_inactive_and_failed() -> None:
    prover, sync = user_services.prover_unit(), user_services.prime_sync_unit()
    fake = FakeSystemd()
    _healthy(fake, prover)
    fake.set(sync.timer, ActiveState="active", SubState="waiting")
    fake.set(
        sync.service,
        ActiveState="failed",
        SubState="failed",
        Result="exit-code",
        ExecMainStatus="2",
        InvocationID="0123456789abcdef0123456789abcdef",
    )
    check = user_services.check_units([prover, sync], run=fake)
    assert [row["state"] for row in check.rows] == ["ok", "failed"]
    assert check.rows[1]["last_error"] == "BLOCKED: HTTP Error 401: Unauthorized"
    assert len(check.issues) == 1 and "verdict-prime-sync.service failed" in check.issues[0]
    assert "401" in check.issues[0]

    fake.set(prover.timer, ActiveState="inactive", SubState="dead")  # the stop drill
    del fake.state[sync.timer]
    check = user_services.check_units([prover, sync], run=fake)
    assert [row["state"] for row in check.rows] == ["inactive", "missing"]
    assert any("verdict-prove-at-rest.timer is inactive" in issue for issue in check.issues)
    assert any("verdict-prime-sync.timer is not installed" in issue for issue in check.issues)


def test_check_units_without_systemd_is_a_warning_not_a_pass() -> None:
    check = user_services.check_units([user_services.prover_unit()])
    assert check.warnings and not check.issues and not check.rows


def _cache(home: Path, checked_at: datetime, healthy: bool = True) -> None:
    cache = HealthCache(home / ".verdict" / "health-cache.json")
    cache.record(
        "free/m",
        ProbeResult("ok", True, True) if healthy else ProbeResult("timeout", False, False),
        checked_at,
    )
    cache.save()


def test_health_cache_age_and_usable_routes(home: Path) -> None:
    check = user_services.check_health_cache(300, now=NOW)
    assert check.issues and "does not exist" in check.issues[0]
    _cache(home, NOW - timedelta(minutes=5))
    assert user_services.check_health_cache(300, now=NOW).issues == []
    stale = user_services.check_health_cache(60, now=NOW)
    assert [issue for issue in stale.issues if "is stale" in issue] == stale.issues != []
    _cache(home, NOW - timedelta(minutes=1), healthy=False)
    later = user_services.check_health_cache(300, now=NOW)
    assert any("no fresh or stale route" in issue for issue in later.issues)


def test_cycle_seconds_read_from_installed_unit(home: Path) -> None:
    budget = user_services.ProberBudget(interval=120, max_wall_seconds=60)
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    for name, body in user_services.prover_unit(budget).files().items():
        (unit_dir / name).write_text(body)
    assert user_services.prober_cycle_seconds() == 180


# -- doctor and setup --check -------------------------------------------------


def _listed(eligible: int, proven: int) -> dict[str, object]:
    return {
        "summary": {"eligible": eligible},
        "health_cache": {"agentic_qualified_by_capacity_class": {"free": proven}},
    }


def test_eligible_list_check_fails_only_when_proofs_list_nothing() -> None:
    assert user_services.check_eligible_list(_listed(0, 0)).issues == []
    assert user_services.check_eligible_list(_listed(3, 13)).issues == []
    stale = user_services.check_eligible_list(_listed(0, 13))
    assert stale.issues and "13 FREE routes have an agentic proof" in stale.issues[0]


@pytest.fixture
def listed(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """The canonical eligibility action, answered from a mutable payload."""
    from verdict.actions.base import ActionResult

    payload: dict[str, object] = _listed(2, 2)
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action

    def run_action(name: str, params: dict[str, object] | None = None) -> ActionResult:
        if name == "eligibility":
            return ActionResult(data=payload)
        return real(name, params or {})

    monkeypatch.setattr("verdict.actions.registry.run_action", run_action)
    return payload


def test_doctor_reports_a_stopped_timer_and_exits_non_zero(
    home: Path, monkeypatch: pytest.MonkeyPatch, listed: dict[str, object]
) -> None:
    from verdict.doctor_diagnostics import DoctorDiagnostics, _doctor_services

    _write_secret(home / ".config/verdict/omniroute-api-key", "k")
    _cache(home, datetime.now(timezone.utc))
    fake = FakeSystemd()
    _healthy(fake, user_services.prover_unit())
    monkeypatch.setattr(user_services, "run_systemctl", fake)
    green = DoctorDiagnostics()
    _doctor_services(green)
    assert green.issues == [] and green.services[0]["state"] == "ok"

    fake.set("verdict-prove-at-rest.timer", ActiveState="inactive", SubState="dead")
    red = DoctorDiagnostics()
    _doctor_services(red)
    assert red.issues and "verdict-prove-at-rest.timer is inactive" in red.issues[0]
    assert ("verdict-prove-at-rest.timer", "inactive") in [(s[0], s[1]) for s in red.sections]

    _healthy(fake, user_services.prover_unit())
    listed.update(_listed(0, 13))  # proofs exist, the list is empty
    stale = DoctorDiagnostics()
    _doctor_services(stale)
    assert [i for i in stale.issues if "Eligibility list is stale" in i]


def test_doctor_flags_units_that_cannot_read_a_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, listed: dict[str, object]
) -> None:
    from verdict.doctor_diagnostics import DoctorDiagnostics, _doctor_services

    monkeypatch.setenv("OMNIROUTE_API_KEY", "shell-only")
    _cache(home, datetime.now(timezone.utc))
    fake = FakeSystemd()
    _healthy(fake, user_services.prover_unit())
    monkeypatch.setattr(user_services, "run_systemctl", fake)
    diag = DoctorDiagnostics()
    _doctor_services(diag)
    assert any("cannot authenticate" in issue for issue in diag.issues)


def test_setup_check_cli_exits_non_zero_when_a_unit_is_absent(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict.cli import main

    fake = FakeSystemd()
    monkeypatch.setattr(user_services, "run_systemctl", fake)
    monkeypatch.setattr("sys.argv", ["verdict", "setup", "--check", "--json"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)["check"]
    assert report["services"][0]["state"] == "missing"


def test_setup_services_cli_installs_with_the_key_file(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict.cli import main

    _write_secret(home / ".config/verdict/omniroute-api-key", "k")
    fake = FakeSystemd()
    _healthy(fake, user_services.prover_unit())
    monkeypatch.setattr(user_services, "run_systemctl", fake)
    monkeypatch.setattr("sys.argv", ["verdict", "setup", "services", "--json"])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["install"]["key_source"] == "key_file"
    assert payload["check"]["status"] == "ok"
    unit = home / ".config/systemd/user/verdict-prove-at-rest.service"
    assert os.path.exists(unit)


def test_setup_check_refuses_other_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from verdict.cli import main

    monkeypatch.setattr("sys.argv", ["verdict", "setup", "credentials", "--check"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "--check checks Verdict services" in capsys.readouterr().err


def test_daemon_reports_each_finished_cycle() -> None:
    from verdict.prove_at_rest import CycleStats, Prober

    done: list[CycleStats] = []
    prober = Prober(
        cache=HealthCache.__new__(HealthCache),
        routes_loader=lambda: [],
        transport=lambda *a: None,  # type: ignore[arg-type,return-value]
        interval_seconds=0.01,
        sleep=lambda _s: None,
    )

    def run_once() -> CycleStats:
        if done:
            prober.stop()
        return CycleStats(requests=3, stopped_reason="complete")

    prober.run_once = run_once  # type: ignore[method-assign]
    prober.on_cycle_done = done.append
    prober.run_forever()
    assert done and done[0].requests == 3
