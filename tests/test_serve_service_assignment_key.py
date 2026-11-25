"""The worker-assignment signing key through ``verdict setup`` and ``verdict doctor`` (group 6).

``manage_service`` generates and keeps ``~/.config/verdict/worker-assignment.key``;
``install_problems`` (which setup and doctor already use for ``serve.env``) names a missing or
bad key.  Every test uses a temporary home, XDG directory and VERDICT_HOME and a recording
``systemctl`` stand-in: nothing touches the operator's ``~/.config`` or ``~/.verdict``.  The key
value is asserted absent from every payload, problem list, env file and credentials store.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests import worker_assignment_helpers as sign
from verdict import serve_service
from verdict import worker_assignment as wa
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


def _manage(home: Path, run: Systemctl | None = None, **kwargs: Any) -> dict[str, Any]:
    return serve_service.manage_service(
        unit_dir=home / ".config" / "systemd" / "user", home=home, run=run or Systemctl(), **kwargs
    )


def _key_file(home: Path) -> Path:
    return wa.assignment_key_path(home)


def _key_text(home: Path) -> str:
    return _key_file(home).read_text(encoding="utf-8").strip()


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _problems(home: Path) -> list[str]:
    return serve_service.install_problems(unit_dir=home / ".config" / "systemd" / "user", home=home)


def _key_problems(home: Path) -> list[str]:
    return [item for item in _problems(home) if wa.KEY_FILE_NAME in item]


# --- manage_service: generate once, keep, dry run, uninstall ------------------------------------


def test_setup_writes_a_0600_key_of_32_random_bytes_in_a_0700_directory(home: Path) -> None:
    result = _manage(home)
    path = _key_file(home)
    assert path.is_file()
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    key = wa.load_assignment_key(home)  # decodes through the one loader serve uses
    assert len(key) >= 32
    assert result["assignment_key_file"] == str(path)
    assert result["assignment_key_plan"] == "create"


def test_setup_run_twice_keeps_the_same_bytes(home: Path) -> None:
    _manage(home)
    first = _key_file(home).read_bytes()
    second_result = _manage(home)
    assert _key_file(home).read_bytes() == first
    assert second_result["assignment_key_plan"] == "keep"
    # Generating a key is not what restarts serve: a second run changes nothing.
    assert second_result["changed"] is False


def _restart_commands(run: Systemctl) -> list[list[str]]:
    return [call for call in run.calls if call[2] in {"try-restart", "daemon-reload"}]


def test_a_key_created_by_itself_is_reported_as_changed_without_restarting_serve(
    home: Path,
) -> None:
    first = _manage(home)
    assert first["changed"] is True  # the unit, the env file and the key were all new
    _key_file(home).unlink()
    run = Systemctl()
    result = _manage(home, run)
    assert result["assignment_key_plan"] == "create"
    assert result["plan"] == {serve_service.SERVICE: "keep"}
    assert result["env_file_plan"] == "keep"
    assert result["changed"] is True  # a new signing key is a change the operator should see
    assert result["service_changed"] is False  # but serve reads the key per request: no restart
    assert _restart_commands(run) == []
    assert run.calls == [["systemctl", "--user", "enable", "--now", serve_service.SERVICE]]
    assert len(wa.load_assignment_key(home)) >= 32


def test_a_replaced_key_is_reported_as_changed_without_restarting_serve(home: Path) -> None:
    _manage(home)
    path = _key_file(home)
    bad = sign.b64url(b"too short")
    path.write_text(bad, encoding="utf-8")
    run = Systemctl()
    result = _manage(home, run)
    assert result["assignment_key_plan"] == "replace"
    assert result["changed"] is True
    assert result["service_changed"] is False
    assert _restart_commands(run) == []
    assert path.read_text(encoding="utf-8").strip() != bad


def test_a_repaired_key_mode_is_reported_as_changed_without_restarting_serve(home: Path) -> None:
    _manage(home)
    path = _key_file(home)
    good = path.read_bytes()
    os.chmod(path, 0o644)
    run = Systemctl()
    result = _manage(home, run)
    assert result["assignment_key_plan"] == "repair-mode"
    assert result["changed"] is True
    assert result["service_changed"] is False
    assert _restart_commands(run) == []
    assert path.read_bytes() == good and _mode(path) == 0o600


def test_a_dry_run_reports_a_pending_key_change_and_still_writes_nothing(home: Path) -> None:
    _manage(home)
    _key_file(home).unlink()
    run = Systemctl()
    result = _manage(home, run, dry_run=True)
    assert result["assignment_key_plan"] == "create"
    assert result["changed"] is True
    assert result["service_changed"] is False
    assert not _key_file(home).exists()
    assert run.calls == []


def test_a_unit_or_env_change_still_restarts_serve_and_reports_service_changed(home: Path) -> None:
    _manage(home)
    unit = home / ".config" / "systemd" / "user" / serve_service.SERVICE
    unit.write_text("[Service]\nExecStart=/old\n", encoding="utf-8")
    run = Systemctl()
    result = _manage(home, run)
    assert result["plan"] == {serve_service.SERVICE: "replace"}
    assert result["assignment_key_plan"] == "keep"
    assert result["changed"] is True
    assert result["service_changed"] is True
    assert ["systemctl", "--user", "try-restart", serve_service.SERVICE] in run.calls


def test_nothing_to_change_reports_neither_changed_nor_service_changed(home: Path) -> None:
    _manage(home)
    run = Systemctl()
    result = _manage(home, run)
    assert result["assignment_key_plan"] == "keep"
    assert result["changed"] is False
    assert result["service_changed"] is False


def test_each_fresh_home_gets_its_own_random_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = []
    for name in ("one", "two"):
        base = tmp_path / name
        base.mkdir()
        monkeypatch.setenv("HOME", str(base))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(base / ".config"))
        monkeypatch.setenv("VERDICT_HOME", str(base / ".verdict"))
        _manage(base)
        keys.append(wa.load_assignment_key(base))
    assert keys[0] != keys[1]


def test_a_dry_run_writes_no_key(home: Path) -> None:
    result = _manage(home, dry_run=True)
    assert not _key_file(home).exists()
    assert not (home / ".config").exists()
    assert result["assignment_key_plan"] == "create"


def test_a_dry_run_leaves_an_existing_key_and_a_bad_one_alone(home: Path) -> None:
    _manage(home)
    good = _key_file(home).read_bytes()
    _manage(home, dry_run=True)
    assert _key_file(home).read_bytes() == good
    _key_file(home).write_text("short", encoding="utf-8")
    result = _manage(home, dry_run=True)
    assert _key_file(home).read_text(encoding="utf-8") == "short"
    assert result["assignment_key_plan"] == "replace"


def test_an_invalid_key_is_replaced_and_a_valid_key_with_a_loose_mode_is_only_repaired(
    home: Path,
) -> None:
    _manage(home)
    path = _key_file(home)
    good = path.read_bytes()
    os.chmod(path, 0o644)
    repaired = _manage(home)
    assert repaired["assignment_key_plan"] == "repair-mode"
    assert path.read_bytes() == good  # the same key, never regenerated for a mode problem
    assert _mode(path) == 0o600

    path.write_text(sign.b64url(b"too short"), encoding="utf-8")
    replaced = _manage(home)
    assert replaced["assignment_key_plan"] == "replace"
    assert len(wa.load_assignment_key(home)) >= 32
    assert path.read_bytes() != good


def test_a_key_path_that_is_not_a_file_is_refused_not_overwritten(home: Path) -> None:
    _key_file(home).mkdir(parents=True)
    with pytest.raises(serve_service.ServeServiceError, match="not a file"):
        _manage(home)
    assert _key_file(home).is_dir()


def test_uninstall_removes_the_key_with_serve_env(home: Path) -> None:
    _manage(home)
    assert _key_file(home).exists()
    dry = _manage(home, uninstall=True, dry_run=True)
    assert _key_file(home).exists() and dry["assignment_key_plan"] == "remove"
    result = _manage(home, uninstall=True)
    assert not _key_file(home).exists()
    assert not (home / ".config" / "verdict" / "serve.env").exists()
    assert result["assignment_key_plan"] == "remove"
    again = _manage(home, uninstall=True)
    assert again["assignment_key_plan"] == "absent"


def test_the_key_is_in_no_env_file_credentials_store_payload_or_problem_list(home: Path) -> None:
    result = _manage(home)
    value = _key_text(home)
    assert len(value) >= 43
    env_text = (home / ".config" / "verdict" / "serve.env").read_text(encoding="utf-8")
    assert value not in env_text
    assert wa.KEY_FILE_NAME not in env_text
    assert value not in json.dumps(CredentialsStore().load())
    assert value not in json.dumps(result, default=str)
    os.chmod(_key_file(home), 0o644)
    assert value not in " ".join(_problems(home))
    # Nothing set the key in a process environment.
    assert not any(value in item for item in os.environ.values())


# --- install_problems: what doctor shows -----------------------------------------------------------


def test_a_fully_installed_serve_has_no_key_problem(home: Path) -> None:
    _manage(home)
    assert _key_problems(home) == []
    assert _problems(home) == []


def test_a_missing_key_file_is_named_even_when_serve_env_is_missing_too(home: Path) -> None:
    problems = _problems(home)
    assert any(wa.KEY_FILE_NAME in item and "missing" in item for item in problems)
    _manage(home)
    _key_file(home).unlink()
    problems = _problems(home)
    assert [item for item in problems if "missing" in item and wa.KEY_FILE_NAME in item]


def test_a_key_under_32_bytes_is_named_and_the_value_is_not(home: Path) -> None:
    _manage(home)
    short = sign.b64url(b"0123456789abcdef")  # 16 bytes
    _key_file(home).write_text(short, encoding="utf-8")
    problems = _key_problems(home)
    assert len(problems) == 1
    assert "32 bytes" in problems[0]
    assert short not in " ".join(_problems(home))


@pytest.mark.parametrize("content", ["", "   \n", "not base64url!", "A" * 9000])
def test_an_empty_undecodable_or_oversized_key_is_named(home: Path, content: str) -> None:
    _manage(home)
    _key_file(home).write_text(content, encoding="utf-8")
    assert len(_key_problems(home)) == 1


def test_a_mode_other_than_0600_is_named_with_the_mode(home: Path) -> None:
    _manage(home)
    os.chmod(_key_file(home), 0o640)
    problems = _key_problems(home)
    assert len(problems) == 1
    assert "0640" in problems[0] and "0600" in problems[0]


def test_install_problems_only_reads(home: Path) -> None:
    """Doctor must not generate, repair or rewrite the key: only setup writes it."""
    _manage(home)
    path = _key_file(home)
    os.chmod(path, 0o644)
    before = (path.read_bytes(), _mode(path), path.stat().st_mtime_ns)
    _problems(home)
    assert (path.read_bytes(), _mode(path), path.stat().st_mtime_ns) == before
    path.unlink()
    _problems(home)
    assert not path.exists()  # a missing key is reported, never created by a read
