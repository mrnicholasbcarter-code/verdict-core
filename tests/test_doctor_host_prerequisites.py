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
    assert dd._prime_sync_timer_repair("missing") == (
        "; repair: verdict harness prime install-service"
    )
    assert repair_command("Prime-sync timer failed x" + dd._prime_sync_timer_repair("missing")) == (
        "verdict harness prime install-service"
    )
    assert dd._prime_sync_timer_repair("inactive") == (
        "; repair: systemctl --user enable --now verdict-prime-sync.timer"
    )
    assert repair_command(dd._prime_sync_timer_repair("failed")) == (
        "verdict harness prime sync-models"
    )


def _ref_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "project": "default/ref-omniroute",
        "tag": "v3.9.0",
        "installed_tag": "v3.9.0",
        "synced_at": "2026-10-16T11:00:00+00:00",
        "pages": 12,
        "ok": True,
        "problems": [],
    }
    return {**row, **overrides}


@pytest.mark.usefixtures("_isolated")
@pytest.mark.parametrize(
    ("state", "result", "repair"),
    [
        ("missing", "unknown", "verdict setup --apply --allow host.ref-docs-timer"),
        ("inactive", "success", "systemctl --user enable --now verdict-ref-docs.timer"),
        ("failed", "exit-code", "verdict memory ref-docs sync"),
        ("active", "exit-code", "verdict memory ref-docs sync"),
        ("active", "unknown", "verdict memory ref-docs sync"),
    ],
)
def test_ref_docs_timer_row_names_its_repair(state: str, result: str, repair: str) -> None:
    def timer_state(unit: str) -> dict[str, object]:
        if unit == "verdict-ref-docs.timer":
            return {"state": state, "result": result}
        return _timer({})(unit)

    rows = dd.installed_service_rows(timer_state=timer_state, ref_docs_status=lambda: [_ref_row()])
    row = {r["label"]: r for r in rows}["Reference-docs timer"]
    assert row["status"] == "failed"
    assert f"verdict-ref-docs.timer is {state}; last result {result}" in row["detail"]
    assert repair_command(row["detail"]) == repair


@pytest.mark.usefixtures("_isolated")
def test_ref_docs_rows_show_each_project_tag_and_last_sync() -> None:
    def timer_state(unit: str) -> dict[str, object]:
        return {"state": "active", "result": "success"} if "ref-docs" in unit else _timer({})(unit)

    rows = {
        r["label"]: r
        for r in dd.installed_service_rows(
            timer_state=timer_state, ref_docs_status=lambda: [_ref_row()]
        )
    }
    assert rows["Reference-docs timer"]["status"] == "ok"
    assert rows["Reference docs default/ref-omniroute"] == {
        "label": "Reference docs default/ref-omniroute",
        "status": "ok",
        "detail": "tag v3.9.0 (installed v3.9.0), last sync 2026-10-16T11:00:00+00:00, 12 pages",
    }

    stale = _ref_row(
        installed_tag="v3.10.0", ok=False, problems=["synced v3.9.0 but v3.10.0 is installed"]
    )
    row = {
        r["label"]: r
        for r in dd.installed_service_rows(timer_state=timer_state, ref_docs_status=lambda: [stale])
    }["Reference docs default/ref-omniroute"]
    assert row["status"] == "failed"
    assert "synced v3.9.0 but v3.10.0 is installed" in row["detail"]
    assert repair_command(row["detail"]) == "verdict memory ref-docs sync"


@pytest.mark.usefixtures("_isolated")
def test_unknown_ref_docs_state_is_never_healthy() -> None:
    """Without injection: no manifest and systemd reports nothing loaded."""
    rows = {r["label"]: r for r in dd.installed_service_rows(timer_state=_timer({}))}
    timer = dd._ref_docs_timer_row(None)
    assert timer["status"] == "failed"
    assert "verdict-ref-docs.timer is missing; last result unknown" in timer["detail"]
    row = rows["Reference docs default/ref-omniroute"]
    assert row["status"] == "failed"
    assert "installed version unknown" in row["detail"] and "never synced" in row["detail"]
