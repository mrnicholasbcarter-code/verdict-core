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

The same action generates the worker-assignment signing key
(``~/.config/verdict/worker-assignment.key``, mode 0600) when it is absent or unusable and keeps a
valid one.  The key is a separate secret from the token: it is never written to ``serve.env``,
the credential store or any process environment, and the payload names its path, never its value.
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
from verdict.worker_assignment import (
    AssignmentError,
    assignment_key_path,
    generate_assignment_key,
    load_assignment_key,
)

SERVICE = "verdict-serve.service"
HOST = "127.0.0.1"
PORT = 8000
BASE_URL = f"http://{HOST}:{PORT}/v1"
HEALTH_URL = f"http://{HOST}:{PORT}/health"
TOKEN_ENV = "LLMGATE_AUTH_TOKEN"
RECEIPTS_ENV = "VERDICT_RECEIPTS_DB"
ENV_FILE_NAME = "serve.env"
#: The origin workers and other clients call by default: the unit's own address.
DEFAULT_ORIGIN = f"http://{HOST}:{PORT}"
#: Overrides :data:`DEFAULT_ORIGIN` for clients (``verdict orchestrate`` workers), the way
#: ``VERDICT_GATEWAY`` overrides the OmniRoute address; ``--serve-url`` wins over it.
SERVE_URL_ENV = "VERDICT_SERVE_URL"
#: Characters an unquoted EnvironmentFile value carries verbatim.
_SAFE_VALUE = re.compile(r"[A-Za-z0-9._~+/=:@-]+")


class ServeServiceError(Exception):
    """The serve unit cannot be rendered safely; nothing was written."""


def startup_fix() -> str:
    """The exact remedy printed when ``verdict serve`` refuses to start."""
    from verdict.paths import verdict_home

    return (
        f"Fix: run 'verdict setup --apply --allow {SERVE_SETUP_ID}' (installs the unit and "
        f"writes both settings), or export {TOKEN_ENV}=<token> and "
        f"{RECEIPTS_ENV}={verdict_home() / 'receipts.db'}; "
        "see docs/CONFIGURATION.md#serve-authentication-and-receipts"
    )


#: The ``verdict setup`` action id that installs this unit (``setup_host.SERVE``).
SERVE_SETUP_ID = "host.serve"


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


def resolve_serve_origin(
    configured: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    """The ``scheme://host:port`` origin of the ``verdict serve`` that workers call.

    ``configured`` (a flag), then ``VERDICT_SERVE_URL``, then the unit's own address
    (:data:`DEFAULT_ORIGIN`).  A path such as ``/v1`` is dropped.  An address that is not an
    http(s) origin raises :class:`ServeServiceError` instead of silently falling back.
    """
    from verdict.orchestration.contracts import normalize_gateway_origin

    source = os.environ if environ is None else environ
    raw = (configured or "").strip() or (source.get(SERVE_URL_ENV) or "").strip()
    origin = normalize_gateway_origin(raw or DEFAULT_ORIGIN)
    if origin is None or not origin.startswith(("http://", "https://")):
        raise ServeServiceError(
            f"the verdict serve address {raw!r} is not an http(s) origin; "
            f"set --serve-url or {SERVE_URL_ENV} to e.g. {DEFAULT_ORIGIN}"
        )
    return origin


def resolve_serve_token(
    environ: Mapping[str, str] | None = None, *, home: Path | None = None
) -> tuple[str, str] | None:
    """The bearer token for ``verdict serve`` and its source, or None.

    The sources and the order ``verdict setup`` uses (:func:`_existing_token`): the
    environment (``LLMGATE_AUTH_TOKEN``), the credential store, then ``serve.env``.  Each source
    is read only when the ones above it gave nothing, and a lower source that cannot be read or
    decoded is skipped, so a malformed file never hides a valid token above it nor raises.  It is
    the token serve itself authenticates with, never the OmniRoute gateway key.  The value is for
    the caller that must present it; it is never logged here.
    """
    from verdict.credentials_store import CredentialsStore

    source = os.environ if environ is None else environ
    value = (source.get(TOKEN_ENV) or "").strip()
    if value:
        return value, "environment"
    try:
        stored = (CredentialsStore().load().get(TOKEN_ENV) or "").strip()
    except (OSError, ValueError):  # unreadable store, or one that does not decode
        stored = ""
    if stored:
        return stored, "credential_store"
    try:
        from_file = (_read_env_file(env_file_path(home)).get(TOKEN_ENV) or "").strip()
    except (OSError, ValueError):
        from_file = ""
    return (from_file, "serve_env") if from_file else None


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


def _assignment_key_step(key_file: Path, home: Path) -> str:
    """What setup does with the signing key, decided by reading only.

    ``create`` (absent), ``keep`` (valid, mode 0600), ``repair-mode`` (valid, another mode: the
    same key is kept and only its mode is fixed) or ``replace`` (present but unusable).  A path
    that is not a file is refused rather than overwritten.
    """
    if not key_file.exists() and not key_file.is_symlink():
        return "create"
    if not key_file.is_file():
        raise ServeServiceError(f"{key_file} is not a file; refusing to overwrite it")
    try:
        load_assignment_key(home)
    except AssignmentError:
        return "replace"
    return "keep" if _mode(key_file) == 0o600 else "repair-mode"


def _assignment_key_problems(key_file: Path, home: Path) -> list[str]:
    """What is wrong with the signing key, read-only.  Names the file, never the value."""
    if not key_file.exists():
        return [f"{key_file} is missing"]
    problems = []
    try:
        load_assignment_key(home)
    except AssignmentError:
        problems.append(
            f"{key_file} is not a usable worker assignment key "
            "(it must hold at least 32 bytes as URL-safe base64 text)"
        )
    if key_file.is_file() and _mode(key_file) != 0o600:
        problems.append(f"{key_file} has mode {_mode(key_file):04o}, not 0600")
    return problems


def manage_service(
    *,
    unit_dir: Path | None = None,
    home: Path | None = None,
    dry_run: bool = False,
    uninstall: bool = False,
    run: Callable[[list[str]], None] = _systemctl,
    environ: Mapping[str, str] | None = None,
    generate: Callable[[int], str] = secrets.token_urlsafe,
    generate_key: Callable[[], str] = generate_assignment_key,
) -> dict[str, Any]:
    """Render or apply the serve unit, its env file and the signing key.

    Dry-run never writes or runs systemctl.  The key is a separate generated secret kept in its
    own file; it needs no restart (serve reads it on every assignment request).
    """
    from verdict.credentials_store import CredentialsStore
    from verdict.paths import verdict_home

    user_home = home or Path.home()
    directory = unit_dir or (
        Path(os.environ.get("XDG_CONFIG_HOME", str(user_home / ".config"))) / "systemd" / "user"
    )
    env_file = env_file_path(user_home)
    key_file = assignment_key_path(user_home)
    source = os.environ if environ is None else environ
    if uninstall:
        plan = plan_units(directory, {SERVICE: ""}, uninstall=True)
        env_step = "remove" if env_file.exists() else "absent"
        key_step = "remove" if key_file.exists() else "absent"
        service_changed = plan[SERVICE] == "remove" or env_step == "remove"
        changed = service_changed or key_step == "remove"
        commands = [["systemctl", "--user", "disable", "--now", SERVICE]] if changed else []
        rollback = env_rollback = None
        if not dry_run:
            rollback = save_rollback(directory, plan)
            env_rollback = save_rollback(env_file.parent, {ENV_FILE_NAME: env_step})
            for args in commands:
                run(args)
            (directory / SERVICE).unlink(missing_ok=True)
            env_file.unlink(missing_ok=True)
            key_file.unlink(missing_ok=True)
            if changed:
                run(["systemctl", "--user", "daemon-reload"])
        return {
            "unit_dir": str(directory),
            "files": {},
            "plan": plan,
            "env_file": str(env_file),
            "env_file_plan": env_step,
            "env_keys": [],
            "assignment_key_file": str(key_file),
            "assignment_key_plan": key_step,
            "changed": changed,
            "service_changed": service_changed,
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
    key_step = _assignment_key_step(key_file, user_home)  # validates before any write
    # Two questions, kept apart: did the unit or env file change (serve must be restarted and
    # systemd reloaded), and did anything change at all (the signing key included, which serve
    # re-reads per request and so never needs a restart).
    service_changed = plan[SERVICE] != "keep" or env_step != "keep"
    changed = service_changed or key_step != "keep"
    commands = [["systemctl", "--user", "enable", "--now", SERVICE]]
    if service_changed:
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
        if key_step in {"create", "replace"}:
            # A separate secret from the token; written atomically, never into serve.env.
            _write_private(key_file, generate_key() + "\n")
        elif key_step == "repair-mode":
            os.chmod(key_file, 0o600)  # the same key: only its mode was wrong
        if store_token:
            store.set(TOKEN_ENV, token)
        if service_changed:
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
        "assignment_key_file": str(key_file),
        "assignment_key_plan": key_step,
        "token_source": token_source,
        "token_stored": bool(store_token and not dry_run),
        "changed": changed,
        "service_changed": service_changed,
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
    key_problems = _assignment_key_problems(assignment_key_path(user_home), user_home)
    if not env_file.is_file():
        return [*problems, f"{env_file} is missing", *key_problems]
    values = _read_env_file(env_file)
    if not values.get(TOKEN_ENV, "").strip():
        problems.append(f"{env_file} has no {TOKEN_ENV}")
    receipts_db = str(verdict_home() / "receipts.db")
    if values.get(RECEIPTS_ENV) != receipts_db:
        problems.append(f"{env_file} {RECEIPTS_ENV} is not {receipts_db}")
    if _mode(env_file) != 0o600:
        problems.append(f"{env_file} has mode {_mode(env_file):04o}, not 0600")
    return [*problems, *key_problems]


def healthy(*, probe: Callable[..., bool] | None = None) -> bool:
    """True when the installed serve answers ``GET /health`` on its loopback port."""
    if probe is None:
        from verdict.harness_codex import probe_health

        probe = probe_health
    return bool(probe(base_url=BASE_URL))
