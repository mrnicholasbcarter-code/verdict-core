"""Explicit, idempotent systemd user unit for ``verdict serve`` (BOD-219).

Same shape as the other Verdict installers (``prove_at_rest_service``,
``acceptance_service``): render, dry-run without writes or systemctl, write
only what changed, copy replaced files for rollback, then ``enable --now``.

The unit binds ``127.0.0.1:8000`` from ``%h`` and reads
``%h/.config/verdict/serve.env``, which holds ``LLMGATE_AUTH_TOKEN`` and
``VERDICT_RECEIPTS_DB`` (authenticated serve requires both). The file is 0600.
The token is reused when one exists (exported, credential store, then the
current env file) and generated with ``secrets.token_urlsafe`` otherwise; a
generated or env-file-only token is saved to the credential store. Token values
never appear in the returned payload, in errors or in logs.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from verdict.prove_at_rest_service import _quote, _systemctl, plan_units, save_rollback

SERVICE = "verdict-serve.service"
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}/v1"
HEALTH_URL = f"http://{HOST}:{PORT}/health"
TOKEN_ENV = "LLMGATE_AUTH_TOKEN"
RECEIPTS_ENV = "VERDICT_RECEIPTS_DB"
ENV_FILE_NAME = "serve.env"
#: Characters an unquoted EnvironmentFile value carries verbatim.
_SAFE_VALUE = re.compile(r"[A-Za-z0-9._~+/=:@-]+")


class ServeServiceError(Exception):
    """The serve unit cannot be rendered safely; nothing was written."""


def env_file_path(home: Path | None = None) -> Path:
    """``~/.config/verdict/serve.env``: the path the unit's ``%h`` form names."""
    return (home or Path.home()) / ".config" / "verdict" / ENV_FILE_NAME


def render_unit(python: str | None = None) -> str:
    interpreter = _quote(python or str(Path(sys.executable).absolute()))
    return (
        "[Unit]\nDescription=Verdict control plane (verdict serve)\n"
        "After=network-online.target\n"
        "[Service]\nType=simple\nWorkingDirectory=%h\n"
        f"EnvironmentFile=%h/.config/verdict/{ENV_FILE_NAME}\n"
        f"ExecStart={interpreter} -m verdict serve --host {HOST} --port {PORT}\n"
        "Restart=on-failure\nRestartSec=5\n"
        "[Install]\nWantedBy=default.target\n"
    )


def _read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        values[key.strip()] = value
    return values


def _env_value(name: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ServeServiceError(f"{name} contains a line break; refusing to write {ENV_FILE_NAME}")
    if _SAFE_VALUE.fullmatch(value):
        return value
    if name == TOKEN_ENV:
        # Never echo the value: name the problem and the repair only.
        raise ServeServiceError(
            f"{TOKEN_ENV} has characters {ENV_FILE_NAME} cannot carry unquoted; "
            f"set a URL-safe token with: verdict credentials set {TOKEN_ENV}"
        )
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_env_file(token: str, receipts_db: Path) -> str:
    return (
        f"# Written by verdict setup for {SERVICE}. Holds a secret: keep mode 0600.\n"
        f"{TOKEN_ENV}={_env_value(TOKEN_ENV, token)}\n"
        f"{RECEIPTS_ENV}={_env_value(RECEIPTS_ENV, str(receipts_db))}\n"
    )


def _existing_token(
    env_file: Path, stored: Mapping[str, str], environ: Mapping[str, str]
) -> tuple[str | None, str]:
    for source, value in (
        ("environment", environ.get(TOKEN_ENV)),
        ("credential_store", stored.get(TOKEN_ENV)),
        ("serve_env", _read_env_file(env_file).get(TOKEN_ENV)),
    ):
        if value and value.strip():
            return value.strip(), source
    return None, "generate"


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.tmp")
    fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    staged.replace(path)


def manage_service(
    *,
    unit_dir: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
    environ: Mapping[str, str] | None = None,
    generate: Callable[[int], str] = secrets.token_urlsafe,
) -> dict[str, Any]:
    """Render or apply the serve unit and its env file. Dry-run never writes or runs systemctl."""
    from verdict.credentials_store import CredentialsStore
    from verdict.paths import verdict_home

    user_home = home or Path.home()
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(user_home / ".config"))) / "systemd" / "user"
    )
    env_file = env_file_path(user_home)
    source = os.environ if environ is None else environ
    if uninstall:
        plan = plan_units(directory, {SERVICE: ""}, uninstall=True)
        env_step = "remove" if env_file.exists() else "absent"
        changed = plan[SERVICE] == "remove" or env_step == "remove"
        commands = [["systemctl", "--user", "disable", "--now", SERVICE]] if changed else []
        rollback = env_rollback = None
        if not dry_run:
            rollback = save_rollback(directory, plan)
            env_rollback = save_rollback(env_file.parent, {ENV_FILE_NAME: env_step})
            for args in commands:
                run(args)
            (directory / SERVICE).unlink(missing_ok=True)
            env_file.unlink(missing_ok=True)
            if changed:
                run(["systemctl", "--user", "daemon-reload"])
        return {
            "unit_dir": str(directory),
            "files": {},
            "plan": plan,
            "env_file": str(env_file),
            "env_file_plan": env_step,
            "env_keys": [],
            "changed": changed,
            "dry_run": dry_run,
            "uninstall": True,
            "commands": commands,
            "rollback": rollback,
            "env_rollback": env_rollback,
        }

    store = CredentialsStore()
    stored = store.load()
    token, token_source = _existing_token(env_file, stored, source)
    receipts_db = verdict_home() / "receipts.db"
    unit = render_unit()
    files = {SERVICE: unit}
    plan = plan_units(directory, files)
    if token is None and not dry_run:
        token, token_source = generate(32), "generated"
    if token is None:
        env_step = "create" if not env_file.exists() else "replace"
        rendered_env = None
    else:
        rendered_env = render_env_file(token, receipts_db)  # validates before any write
        current = env_file.read_text(encoding="utf-8") if env_file.exists() else None
        private = current is not None and _mode(env_file) == 0o600
        env_step = (
            "create"
            if current is None
            else "keep"
            if current == rendered_env and private
            else "replace"
        )
    changed = plan[SERVICE] != "keep" or env_step != "keep"
    commands = [["systemctl", "--user", "enable", "--now", SERVICE]]
    if changed:
        commands.append(["systemctl", "--user", "try-restart", SERVICE])
    store_token = token is not None and not stored.get(TOKEN_ENV, "").strip()
    rollback = env_rollback = None
    if not dry_run and rendered_env is not None and token is not None:
        rollback = save_rollback(directory, plan)
        env_rollback = save_rollback(env_file.parent, {ENV_FILE_NAME: env_step})
        directory.mkdir(parents=True, exist_ok=True)
        if plan[SERVICE] != "keep":
            (directory / SERVICE).write_text(unit)
        if env_step != "keep":
            _write_private(env_file, rendered_env)
        if store_token:
            store.set(TOKEN_ENV, token)
        if changed:
            run(["systemctl", "--user", "daemon-reload"])
        for args in commands:
            run(args)
    return {
        "unit_dir": str(directory),
        "files": files,
        "plan": plan,
        "env_file": str(env_file),
        "env_file_plan": env_step,
        "env_keys": [TOKEN_ENV, RECEIPTS_ENV],
        "token_source": token_source,
        "token_stored": bool(store_token and not dry_run),
        "changed": changed,
        "dry_run": dry_run,
        "uninstall": False,
        "commands": commands,
        "rollback": rollback,
        "env_rollback": env_rollback,
    }


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def install_problems(*, unit_dir: Path | None = None, home: Path | None = None) -> list[str]:
    """What an install would change, read-only. Names keys, never their values."""
    from verdict.paths import verdict_home

    user_home = home or Path.home()
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(user_home / ".config"))) / "systemd" / "user"
    )
    unit_step = plan_units(directory, {SERVICE: render_unit()})[SERVICE]
    problems = []
    if unit_step != "keep":
        word = "missing" if unit_step == "create" else "not the rendered unit"
        problems.append(f"{directory / SERVICE} is {word}")
    env_file = env_file_path(user_home)
    if not env_file.is_file():
        return [*problems, f"{env_file} is missing"]
    values = _read_env_file(env_file)
    if not values.get(TOKEN_ENV, "").strip():
        problems.append(f"{env_file} has no {TOKEN_ENV}")
    receipts_db = str(verdict_home() / "receipts.db")
    if values.get(RECEIPTS_ENV) != receipts_db:
        problems.append(f"{env_file} {RECEIPTS_ENV} is not {receipts_db}")
    if _mode(env_file) != 0o600:
        problems.append(f"{env_file} has mode {_mode(env_file):04o}, not 0600")
    return problems


def healthy(*, probe: Callable[..., bool] | None = None) -> bool:
    """True when the installed serve answers ``GET /health`` on its loopback port."""
    if probe is None:
        from verdict.harness_codex import probe_health

        probe = probe_health
    return bool(probe(base_url=BASE_URL))
