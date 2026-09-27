"""Supervisor wiring: live admission loader and active-controller identity export."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_prime_supervisor import _git_init, _write_controller_decision, module


def test_run_attempt_passes_explicit_env_to_child(tmp_path: Path) -> None:
    m = module()
    result = m.run_attempt(
        [sys.executable, "-c", "import os; print(os.environ['VERDICT_ACTIVE_CONTROLLER_ROUTE'])"],
        tmp_path,
        tmp_path / "out.log",
        lambda: "x",
        5,
        5,
        0.02,
        env={"VERDICT_ACTIVE_CONTROLLER_ROUTE": "kr/ctrl", "PATH": "/usr/bin:/bin"},
    )
    assert result["reason"] == "EXIT" and result["returncode"] == 0
    assert (tmp_path / "out.log").read_text().strip() == "kr/ctrl"


def test_config_built_service_gets_live_admission_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = module()
    captured: dict[str, Any] = {}

    def fake_bundle(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(hooks=None, artifacts=SimpleNamespace(last_compiled=None))

    monkeypatch.setattr(m.CS, "build_production_controller_selection_bundle", fake_bundle)
    service = SimpleNamespace(require_execution_path_authority=True)
    m.build_production_controller_selection_bundle(
        repo=tmp_path,
        state_dir=tmp_path / "state",
        intelligence_service=service,
        bind_prime_target=lambda route: None,
    )
    loader = captured.get("admission")
    assert callable(loader)

    import verdict.admission as adm

    def unavailable(*a: Any, **k: Any) -> Any:
        raise adm.AdmissionUnavailableError("connection_evidence_unavailable", "down")

    monkeypatch.setattr(adm, "load_live_admission", unavailable)
    with pytest.raises(adm.AdmissionUnavailableError):
        loader(None)


def test_supervisor_exports_active_controller_to_launched_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    prompt = "COMPILED\n/skill:verdict-resume\n"
    digest = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
    decision_path = _write_controller_decision(
        tmp_path / "d.json", provider="omniroute", model="kr/ctrl"
    )
    payload = json.loads(decision_path.read_text())
    payload["selected_prompt_digest"] = digest
    decision_path.write_text(json.dumps(payload))
    envs: list[dict[str, str]] = []

    def fake_factory(**kwargs: Any) -> Any:
        return SimpleNamespace(
            hooks=object(),
            artifacts=SimpleNamespace(
                last_compiled=SimpleNamespace(route_id="omniroute/kr/ctrl", compiled_prompt=prompt),
                last_admission=None,
            ),
        )

    def fake_resolve(**kwargs: Any) -> Any:
        persisted = m.load_persisted_authoritative_decision(decision_path)
        mission = m.load_controller_mission(None, attempt_id=kwargs["attempt_id"])
        return m.decide_controller_launch(mission, persisted=persisted, now=kwargs.get("now"))

    def fake_run_attempt(command: list[str], *args: Any, **kwargs: Any) -> Any:
        envs.append(dict(kwargs.get("env") or {}))
        token = Path(command[command.index("--session-dir") + 1]).name
        (state / "outcome.json").write_text(
            json.dumps({"run_id": token, "status": "DONE", "reason": "ok"})
        )
        return {"reason": "EXIT", "returncode": 0}

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", fake_factory)
    monkeypatch.setattr(m, "resolve_controller_decision", fake_resolve)
    monkeypatch.setattr(m, "load_session_state_from_prior", lambda **k: None)
    monkeypatch.setattr(m, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            "prime-agent",
            "--skip-identity-verify",
            "--max-restarts",
            "0",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
        ],
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    assert m.main() == 0
    assert envs and envs[0]["VERDICT_ACTIVE_CONTROLLER_ROUTE"] == "omniroute/kr/ctrl"
    import os

    assert "VERDICT_ACTIVE_CONTROLLER_ROUTE" not in os.environ


def _yaml_only_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway: str) -> None:
    home = tmp_path / "home"
    cfg = home / ".config" / "verdict"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    for name in ("OMNIROUTE_BASE_URL", "OMNIROUTE_API_KEY", "LLMGATE_PRIMARY"):
        monkeypatch.delenv(name, raising=False)
    (cfg / "verdict.yaml").write_text(
        "primary_model: kr/p\n"
        f"gateway_url: {gateway}\n"
        "providers:\n"
        "  omniroute:\n"
        f"    base_url: {gateway}/v1\n"
        "    api_key_env: OMNIROUTE_API_KEY\n",
        encoding="utf-8",
    )


def test_live_admission_gateway_comes_from_the_bootstrap_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-only operator gets the configured gateway, not a hardcoded loopback default."""
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    m = module()
    seen: list[str] = []

    import verdict.admission as adm

    def fake_load(gateway: str, **kwargs: Any) -> Any:
        seen.append(gateway)
        raise adm.AdmissionUnavailableError("connection_evidence_unavailable", "stop")

    monkeypatch.setattr(adm, "load_live_admission", fake_load)
    loader = m._default_live_admission_loader(tmp_path / "state")
    with pytest.raises(adm.AdmissionUnavailableError):
        loader(None)
    assert seen == ["http://127.0.0.1:29999"]


def test_live_admission_gateway_follows_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _yaml_only_home(tmp_path, monkeypatch, "http://127.0.0.1:29999")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:28888")
    m = module()
    assert m._live_admission_gateway() == "http://127.0.0.1:28888"


def test_live_admission_gateway_unresolved_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No configured gateway and no env: admission is unavailable, never a guessed URL."""
    home = tmp_path / "home"
    (home / ".config" / "verdict").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    m = module()
    import verdict.admission as adm

    with pytest.raises(adm.AdmissionUnavailableError) as exc:
        m._live_admission_gateway()
    assert exc.value.reason == "live_admission_gateway_unresolved"
