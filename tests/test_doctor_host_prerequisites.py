"""``verdict doctor`` host-prerequisite rows and their exact repairs (BOD-219 part 2).

Rows come from ``verdict.setup_host.doctor_rows`` on the same injected
``HostEnvironment`` setup uses (temporary HOME, fake systemd state, fake serve
health), so nothing here reads the real host or opens a connection.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tests.test_setup_host import ASSET, GATEWAY, FakeHost, installed_package_env
from verdict import doctor_diagnostics as dd
from verdict import harness_prime, ocr_release, setup_host
from verdict.doctor_presentation import repair_command


@pytest.fixture
def host(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    return FakeHost(tmp_path, monkeypatch)


def _rows(host: FakeHost) -> dict[str, dict[str, str]]:
    return {row["label"]: row for row in setup_host.doctor_rows(host.env)}


def _install_ocr(host: FakeHost, body: bytes = ASSET) -> None:
    host.ocr.parent.mkdir(parents=True, exist_ok=True)
    host.ocr.write_bytes(body)
    host.ocr.chmod(0o755)


def test_missing_ocr_fails_with_the_setup_repair(host: FakeHost) -> None:
    row = _rows(host)["Review tool (ocr)"]
    assert row["status"] == "failed"
    assert "not on PATH" in row["detail"]
    assert repair_command(row["detail"]) == "verdict setup --apply --allow host.ocr"


def test_wrong_ocr_version_and_digest_mismatch_fail(host: FakeHost) -> None:
    _install_ocr(host, b"another release\n")
    host_env = host.env
    object.__setattr__(host_env, "ocr_version", lambda path: "1.13.0")
    row = _rows(host)["Review tool (ocr)"]
    assert row["status"] == "failed" and "reports v1.13.0" in row["detail"]

    object.__setattr__(host_env, "ocr_version", lambda path: "1.12.9")
    row = _rows(host)["Review tool (ocr)"]
    assert row["status"] == "failed"
    assert f"sha256 {hashlib.sha256(b'another release' + bytes([10])).hexdigest()}" in row["detail"]
    assert repair_command(row["detail"]) == "verdict setup --apply --allow host.ocr"


def test_pinned_ocr_is_ok(host: FakeHost) -> None:
    _install_ocr(host)
    assert _rows(host)["Review tool (ocr)"]["status"] == "ok"


def test_installed_package_reports_missing_ocr_against_the_pin(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live defect: a non-editable install showed ``warning | cannot verify ocr: [Errno 2]``."""
    version, _ = ocr_release.ocr_pin()
    rows = {
        row["label"]: row
        for row in setup_host.doctor_rows(installed_package_env(host, monkeypatch))
    }
    row = rows["Review tool (ocr)"]
    assert row["status"] == "failed", row
    assert row["detail"].startswith("not on PATH; ") and f"pins {version}" in row["detail"]
    assert repair_command(row["detail"]) == "verdict setup --apply --allow host.ocr"


def test_missing_serve_unit_fails_without_probing_health(host: FakeHost) -> None:
    probes: list[int] = []
    object.__setattr__(host.env, "serve_health", lambda: probes.append(1) or True)
    rows = _rows(host)
    row = rows["Verdict serve unit"]
    assert row["status"] == "failed" and "not installed" in row["detail"]
    assert repair_command(row["detail"]) == "verdict setup --apply --allow host.serve"
    assert "Verdict serve health" not in rows and probes == []


def test_inactive_serve_unit_names_enable_now(host: FakeHost) -> None:
    object.__setattr__(
        host.env, "unit_state", lambda unit: {"state": "inactive", "installed": True}
    )
    row = _rows(host)["Verdict serve unit"]
    assert row["status"] == "failed"
    assert repair_command(row["detail"]) == "systemctl --user enable --now verdict-serve.service"
    assert "journalctl --user -u verdict-serve.service" in row["detail"]


def test_unreachable_health_names_restart(host: FakeHost) -> None:
    host.active.add("verdict-serve.service")
    host.serve_up = False
    rows = _rows(host)
    assert rows["Verdict serve unit"]["status"] == "ok"
    row = rows["Verdict serve health"]
    assert row["status"] == "failed" and "http://127.0.0.1:8000/health" in row["detail"]
    assert repair_command(row["detail"]) == "systemctl --user restart verdict-serve.service"
    host.serve_up = True
    assert _rows(host)["Verdict serve health"]["status"] == "ok"


def test_prime_registry_rows_name_each_repair(host: FakeHost) -> None:
    row = _rows(host)["Prime registry"]
    assert row["status"] == "failed" and "missing" in row["detail"]
    assert repair_command(row["detail"]) == "verdict setup --apply --allow host.prime-registry"

    harness_prime.create_omniroute_registry(gateway=GATEWAY)
    row = _rows(host)["Prime registry"]
    assert row["status"] == "failed" and "no models" in row["detail"]
    assert repair_command(row["detail"]) == "verdict harness prime sync-models"

    host.models.write_text(json.dumps({"providers": {"other": {}}}))
    row = _rows(host)["Prime registry"]
    assert row["status"] == "failed" and "no providers.omniroute" in row["detail"]
    assert repair_command(row["detail"]) == "verdict harness prime sync-models"

    host.models.unlink()
    harness_prime.create_omniroute_registry(gateway=GATEWAY)
    host.sync(GATEWAY)
    row = _rows(host)["Prime registry"]
    assert row["status"] == "ok" and "1 model(s)" in row["detail"]


def test_failing_prime_sync_service_names_the_repair(host: FakeHost) -> None:
    assert "Prime-sync service" not in _rows(host)
    object.__setattr__(host.env, "unit_result", lambda unit: "exit-code")
    row = _rows(host)["Prime-sync service"]
    assert row["status"] == "failed" and "exit-code" in row["detail"]
    assert repair_command(row["detail"]) == "verdict harness prime sync-models"
    object.__setattr__(host.env, "unit_result", lambda unit: "success")
    assert _rows(host)["Prime-sync service"]["status"] == "ok"


def test_doctor_rows_use_the_host_environment(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(setup_host.HostEnvironment, "from_host", classmethod(lambda cls: host.env))
    labels = [row["label"] for row in dd.host_prerequisite_rows()]
    assert labels == ["Review tool (ocr)", "Verdict serve unit", "Prime registry"]


# --- rows in installed_service_rows ---------------------------------------------------


def _timer(states: dict[str, str]) -> Any:
    def timer_state(unit: str) -> dict[str, object]:
        state = states.get(unit, "active")
        return {
            "state": state,
            "installed": state != "missing",
            "active": state == "active",
            "failed": state == "failed",
            "result": "success",
            "last_trigger": "Fri 2026-10-16 11:55:00 UTC",
            "last_completion": "Fri 2026-10-16 11:56:00 UTC",
        }

    return timer_state


@pytest.fixture
def _isolated(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))


@pytest.mark.usefixtures("_isolated")
def test_acceptance_timer_row_names_its_installer() -> None:
    rows = {r["label"]: r for r in dd.installed_service_rows(timer_state=_timer({}))}
    assert rows["Acceptance timer"]["status"] == "ok"
    missing = _timer({"verdict-acceptance.timer": "missing"})
    row = {r["label"]: r for r in dd.installed_service_rows(timer_state=missing)}[
        "Acceptance timer"
    ]
    assert row["status"] == "failed"
    assert repair_command(row["detail"]) == "verdict acceptance install-service"


@pytest.mark.usefixtures("_isolated")
def test_metadata_rows_name_the_exact_repair() -> None:
    absent = _timer({"verdict-metadata-refresh.timer": "missing"})
    row = {r["label"]: r for r in dd.installed_service_rows(timer_state=absent)}[
        "Metadata refresh timer"
    ]
    assert row["status"] == "warning"
    assert "not installed" in row["detail"] and "explicit opt-in" in row["detail"]
    assert repair_command(row["detail"]) == "verdict metadata install-service"

    inactive = _timer({"verdict-metadata-refresh.timer": "inactive"})
    row = {r["label"]: r for r in dd.installed_service_rows(timer_state=inactive)}[
        "Metadata refresh timer"
    ]
    assert row["status"] == "failed"
    assert repair_command(row["detail"]) == "verdict metadata install-service"

    row = {r["label"]: r for r in dd.installed_service_rows(timer_state=_timer({}))}[
        "Metadata refresh timer"
    ]
    assert row["status"] == "failed" and "snapshot missing" in row["detail"]
    assert repair_command(row["detail"]) == "verdict metadata refresh"


@pytest.mark.usefixtures("_isolated")
def test_identify_and_prime_sync_timer_rows_name_their_repair() -> None:
    states = {"verdict-identify.timer": "missing", "verdict-prime-sync.timer": "failed"}
    rows = {r["label"]: r for r in dd.installed_service_rows(timer_state=_timer(states))}
    assert repair_command(rows["Identify timer"]["detail"]) == "verdict identify install-service"
    assert repair_command(rows["Prime-sync timer"]["detail"]) == "verdict harness prime sync-models"


def test_explicit_repair_wins_over_keyword_guesses() -> None:
    finding = "Prime registry failed models.json is missing (apiKey OMNIROUTE_API_KEY); "
    finding += "repair: verdict setup --apply --allow host.prime-registry"
    assert repair_command(finding) == "verdict setup --apply --allow host.prime-registry"
    assert repair_command("Gateway unreachable") == "verdict detect"


def test_a_health_probe_exception_is_a_failed_row(host: FakeHost) -> None:
    host.active.add("verdict-serve.service")
    host.serve_up = TimeoutError("probe hung")
    row = _rows(host)["Verdict serve health"]
    assert row["status"] == "failed" and "TimeoutError" in row["detail"]
    assert repair_command(row["detail"]) == "systemctl --user restart verdict-serve.service"


def test_a_missing_prime_sync_timer_names_the_documented_install() -> None:
    from verdict.ocr_release import package_root

    command = (
        f"cp {package_root()}/deploy/systemd/verdict-prime-sync.{{service,timer}} "
        "~/.config/systemd/user/ && systemctl --user daemon-reload && "
        "systemctl --user enable --now verdict-prime-sync.timer"
    )
    assert dd._prime_sync_timer_repair("missing") == (
        f"; repair: {command}; then check VERDICT_CHECKOUT and WorkingDirectory "
        "(docs/guides/harnesses.md)"
    )
    assert repair_command("Prime-sync timer failed x" + dd._prime_sync_timer_repair("missing")) == (
        command
    )
    assert dd._prime_sync_timer_repair("inactive") == (
        "; repair: systemctl --user enable --now verdict-prime-sync.timer"
    )
    assert repair_command(dd._prime_sync_timer_repair("failed")) == (
        "verdict harness prime sync-models"
    )
