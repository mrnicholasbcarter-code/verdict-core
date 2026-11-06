"""verdict/serve_service.py: the ``verdict serve`` systemd user unit (BOD-219 part 2).

Each test uses a temporary HOME, XDG_CONFIG_HOME and VERDICT_HOME, a temporary
unit directory and a recording ``systemctl`` stand-in: nothing here touches the
operator's ``~/.config`` or runs systemctl. Token values are asserted absent
from every returned payload.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from verdict import serve_service
from verdict.credentials_store import CredentialsStore


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    return home


class Systemctl:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> None:
        self.calls.append(list(args))


def _manage(home: Path, run: Systemctl, **kwargs: object) -> dict[str, object]:
    return serve_service.manage_service(
        unit_dir=home / ".config" / "systemd" / "user", home=home, run=run, **kwargs
    )


def _env(home: Path) -> dict[str, str]:
    path = home / ".config" / "verdict" / "serve.env"
    pairs = [
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    ]
    return {key: value for key, value in pairs}


def test_unit_binds_loopback_8000_from_home_with_the_env_file() -> None:
    unit = serve_service.render_unit(python="/opt/verdict/bin/python")
    assert 'ExecStart="/opt/verdict/bin/python" -m verdict serve --host 127.0.0.1 --port 8000' in (
        unit
    )
    assert "\nWorkingDirectory=%h\n" in unit
    assert "\nEnvironmentFile=%h/.config/verdict/serve.env\n" in unit
    assert "\nRestart=on-failure\n" in unit
    assert "WantedBy=default.target" in unit


def test_dry_run_writes_nothing_and_generates_no_token(home: Path) -> None:
    run = Systemctl()
    result = _manage(home, run, dry_run=True)
    assert run.calls == []
    assert not (home / ".config").exists()
    assert result["plan"] == {serve_service.SERVICE: "create"}
    assert result["env_file_plan"] == "create"
    assert result["token_source"] == "generate"
    assert result["env_keys"] == ["LLMGATE_AUTH_TOKEN", "VERDICT_RECEIPTS_DB"]
    assert result["commands"] == [
        ["systemctl", "--user", "enable", "--now", serve_service.SERVICE],
        ["systemctl", "--user", "try-restart", serve_service.SERVICE],
    ]


def test_apply_generates_a_token_stores_it_and_writes_a_private_env_file(home: Path) -> None:
    run = Systemctl()
    result = _manage(home, run)

    env_file = home / ".config" / "verdict" / "serve.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    values = _env(home)
    assert set(values) == {"LLMGATE_AUTH_TOKEN", "VERDICT_RECEIPTS_DB"}
    token = values["LLMGATE_AUTH_TOKEN"]
    assert len(token) >= 32
    assert values["VERDICT_RECEIPTS_DB"] == str(home / ".verdict" / "receipts.db")
    assert CredentialsStore().load()["LLMGATE_AUTH_TOKEN"] == token
    assert result["token_source"] == "generated" and result["token_stored"] is True
    assert token not in json.dumps(result)

    unit = home / ".config" / "systemd" / "user" / serve_service.SERVICE
    assert unit.read_text() == result["files"][serve_service.SERVICE]  # type: ignore[index]
    assert run.calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", serve_service.SERVICE],
        ["systemctl", "--user", "try-restart", serve_service.SERVICE],
    ]


@pytest.mark.parametrize("source", ["environment", "credential_store", "serve_env"])
def test_apply_reuses_an_existing_token(
    home: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    existing = "existing-token-value-0123456789abcdef"
    if source == "environment":
        monkeypatch.setenv("LLMGATE_AUTH_TOKEN", existing)
    elif source == "credential_store":
        CredentialsStore().set("LLMGATE_AUTH_TOKEN", existing)
    else:
        env_file = home / ".config" / "verdict" / "serve.env"
        env_file.parent.mkdir(parents=True)
        env_file.write_text(f"LLMGATE_AUTH_TOKEN={existing}\n")
        env_file.chmod(0o600)

    result = _manage(home, Systemctl())
    assert _env(home)["LLMGATE_AUTH_TOKEN"] == existing
    assert result["token_source"] == source
    assert CredentialsStore().load()["LLMGATE_AUTH_TOKEN"] == existing
    assert existing not in json.dumps(result)


def test_environment_token_wins_and_a_different_stored_token_is_not_overwritten(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    CredentialsStore().set("LLMGATE_AUTH_TOKEN", "stored-token-aaaaaaaaaaaaaaaaaaaa")
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "exported-token-bbbbbbbbbbbbbbbbbbbb")
    result = _manage(home, Systemctl())
    assert _env(home)["LLMGATE_AUTH_TOKEN"] == "exported-token-bbbbbbbbbbbbbbbbbbbb"
    assert CredentialsStore().load()["LLMGATE_AUTH_TOKEN"] == "stored-token-aaaaaaaaaaaaaaaaaaaa"
    assert result["token_stored"] is False


def test_second_apply_is_idempotent(home: Path) -> None:
    _manage(home, Systemctl())
    run = Systemctl()
    result = _manage(home, run)
    assert result["changed"] is False
    assert result["plan"] == {serve_service.SERVICE: "keep"}
    assert result["env_file_plan"] == "keep"
    assert run.calls == [["systemctl", "--user", "enable", "--now", serve_service.SERVICE]]


def test_replaced_unit_and_env_file_are_copied_for_rollback(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / serve_service.SERVICE).write_text("[Service]\nExecStart=/old\n")
    env_file = home / ".config" / "verdict" / "serve.env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("LLMGATE_AUTH_TOKEN=hand-made-token-cccccccccccccccc\nOTHER=1\n")
    env_file.chmod(0o600)

    result = _manage(home, Systemctl())
    assert result["plan"] == {serve_service.SERVICE: "replace"}
    assert result["env_file_plan"] == "replace"
    unit_backup = Path(str(result["rollback"])) / serve_service.SERVICE
    assert unit_backup.read_text() == "[Service]\nExecStart=/old\n"
    env_backup = Path(str(result["env_rollback"])) / "serve.env"
    assert "OTHER=1" in env_backup.read_text()
    assert stat.S_IMODE(env_backup.stat().st_mode) == 0o600
    assert _env(home)["LLMGATE_AUTH_TOKEN"] == "hand-made-token-cccccccccccccccc"


def test_a_token_the_env_file_cannot_carry_fails_closed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLMGATE_AUTH_TOKEN", "bad token\nINJECTED=1")
    run = Systemctl()
    with pytest.raises(serve_service.ServeServiceError) as error:
        _manage(home, run)
    assert "bad token" not in str(error.value)
    assert run.calls == []
    assert not (home / ".config" / "verdict" / "serve.env").exists()


def test_uninstall_removes_the_unit_and_env_file(home: Path) -> None:
    _manage(home, Systemctl())
    run = Systemctl()
    result = _manage(home, run, uninstall=True)
    assert not (home / ".config" / "systemd" / "user" / serve_service.SERVICE).exists()
    assert not (home / ".config" / "verdict" / "serve.env").exists()
    assert run.calls[0] == ["systemctl", "--user", "disable", "--now", serve_service.SERVICE]
    assert result["changed"] is True


def test_unit_state_and_health_are_injectable() -> None:
    def show(args: list[str]) -> object:
        import subprocess

        return subprocess.CompletedProcess(args, 0, "LoadState=loaded\nActiveState=active\n", "")

    assert serve_service.unit_state(run=show)["state"] == "active"  # type: ignore[arg-type]
    urls: list[str] = []

    def probe(*, base_url: str) -> bool:
        urls.append(base_url)
        return True

    assert serve_service.healthy(probe=probe) is True
    assert urls == ["http://127.0.0.1:8000/v1"]
