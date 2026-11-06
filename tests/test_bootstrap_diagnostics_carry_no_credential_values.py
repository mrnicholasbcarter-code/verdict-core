"""No bootstrap diagnostic carries a credential value.

``BootstrapDiagnostic.describe()`` redacts secret shapes, but a value with no
label and no known prefix gets through (pinned in
``tests/test_diagnostic_credential_name_redaction.py``). The real guarantee is
that no diagnostic is built from a credential value in the first place.

Which names get which fake values, exactly:

* every name in the credential registry and in ``GATEWAY_KEY_ENV_NAMES`` that
  is not URL-valued, plus ``FAKE_ENV_API_KEY`` (declared by the test config):
  one value in the exported environment and a different one in the credential
  store, where the environment value wins;
* ``FAKE_STORE_ONLY_API_KEY`` (declared by the test config): a value in the
  credential store only, so the store is the source in force;
* ``FAKE_UNSET_API_KEY`` (declared by the test config): deliberately no value
  anywhere, so ``credential_env_missing`` fires. It has no value to leak;
* URL-valued registry names (``*_BASE_URL``, ``*_API_BASE``): a URL with a fake
  userinfo password in the exported environment. The URL is shown by design,
  the password must not be;
* the gateway key also goes on the gateway start command line, and in the
  ``verdict route`` case into the owner-only key file and Prime's ``auth.json``.

Some paths unset names on purpose to reach a missing-input diagnostic: the
gateway key names (``credential_env_missing``), ``OMNIROUTE_MANAGEMENT_TOKEN``
(``runtime_sources_health_only``), ``OMNIROUTE_BASE_URL`` (no providers) and the
other URL names in the serve case. Every test here also checks that each
credential name that reaches a rendered line has a fake value in some source,
or is ``FAKE_UNSET_API_KEY``.

Then every ``BootstrapDiagnostic`` construction path reachable through a public
entry point is triggered and rendered: ``describe()``, ``to_dict()``,
``BootstrapError`` messages, ``describe_bootstrap_failure``, lifecycle outcomes,
and the stderr and exception text of ``verdict route``, the library gate,
``verdict serve`` and the Prime supervisor. No fake value may appear anywhere.

The fake values have no secret shape on purpose (no ``sk-``, no label, no
``Bearer``), so redaction cannot hide a leak: a leak fails this test.

Offline only (``no_gateway_network``). Every value below is fake.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from verdict import gateway_lifecycle as gl
from verdict.credentials_registry import CREDENTIALS
from verdict.credentials_store import GATEWAY_KEY_ENV_NAMES, GATEWAY_KEY_FILENAME
from verdict.provider_bootstrap import (
    BootstrapDiagnostic,
    BootstrapError,
    BootstrapResult,
    GatewayProbeResult,
    describe_bootstrap_failure,
    resolve_provider_bootstrap,
    verify_gateway_reachable,
)
from verdict.proxy import UpstreamProxy

ROOT = Path(__file__).resolve().parents[1]

ENV_ONLY_KEY = "FAKE_ENV_API_KEY"
STORE_ONLY_KEY = "FAKE_STORE_ONLY_API_KEY"
UNSET_KEY = "FAKE_UNSET_API_KEY"


def _is_url_name(name: str) -> bool:
    return name.endswith(("_BASE_URL", "_API_BASE"))


KNOWN_NAMES = sorted({*GATEWAY_KEY_ENV_NAMES, *(spec.env_name for spec in CREDENTIALS)})
SECRET_NAMES = [name for name in KNOWN_NAMES if not _is_url_name(name)] + [ENV_ONLY_KEY]
URL_NAMES = [name for name in KNOWN_NAMES if _is_url_name(name)]
#: Declared by the test config and given no value anywhere, on purpose.
DELIBERATELY_UNSET = frozenset({UNSET_KEY})
#: An environment-style credential or endpoint name, as it appears in a line.
CREDENTIAL_NAME = re.compile(
    r"\b[A-Z][A-Z0-9_]*_(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|BASE_URL|API_BASE)\b"
)


def _fake(kind: str, index: int) -> str:
    """Unique, shapeless and never a substring of another fake value."""
    return f"fake{kind}{index:02d}qzxkvbnwmtrp"


@dataclasses.dataclass
class Fakes:
    env: dict[str, str]
    store: dict[str, str]
    values: set[str]
    home: Path
    tmp_path: Path

    @property
    def gateway_key(self) -> str:
        return self.env["OMNIROUTE_API_KEY"]

    @property
    def supplied(self) -> set[str]:
        """Names that have a fake value in at least one source."""
        return set(self.env) | set(self.store)

    def without(self, *names: str) -> dict[str, str]:
        return {k: v for k, v in self.env.items() if k not in names}

    def config(self, name: str, text: str) -> Path:
        path = self.tmp_path / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        return path


@pytest.fixture
def fakes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_gateway_network: None) -> Fakes:
    home = tmp_path / "home"
    config_dir = home / ".config" / "verdict"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_HOME", str(home / ".verdict"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    for name in (*KNOWN_NAMES, ENV_ONLY_KEY, STORE_ONLY_KEY, UNSET_KEY):
        monkeypatch.delenv(name, raising=False)
    for name in ("PRIME_AGENT_HOME", "PRIME_HOME", "PRIME_AGENT_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("VERDICT_PRIME_TARGET_MAP", raising=False)

    env: dict[str, str] = {"LLMGATE_LOG_PATH": str(tmp_path / "decisions.jsonl")}
    store: dict[str, str] = {}
    values: set[str] = set()
    for index, name in enumerate(SECRET_NAMES):
        env[name] = _fake("env", index)
        store[name] = _fake("store", index)
        values.update((env[name], store[name]))
    for index, name in enumerate(URL_NAMES):
        password = _fake("urlpw", index)
        env[name] = f"http://fakeuser:{password}@127.0.0.1:20128"
        values.add(password)
    store[STORE_ONLY_KEY] = _fake("storeonly", 0)
    values.add(store[STORE_ONLY_KEY])
    return Fakes(env=env, store=store, values=values, home=home, tmp_path=tmp_path)


class Rendered:
    """Everything a diagnostic path rendered, plus the codes it reached."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.codes: set[str] = set()

    def diagnostic(self, diagnostic: BootstrapDiagnostic | None) -> None:
        if diagnostic is None:
            return
        self.codes.add(diagnostic.code)
        self.texts += [diagnostic.describe(), json.dumps(diagnostic.to_dict())]

    def refusal(self, exc: BootstrapError) -> None:
        for diagnostic in exc.diagnostics:
            self.diagnostic(diagnostic)
        self.texts += [str(exc), describe_bootstrap_failure(exc), json.dumps(exc.to_dict())]

    def result(self, result: BootstrapResult) -> None:
        for diagnostic in result.diagnostics:
            self.diagnostic(diagnostic)
        self.texts.append(json.dumps(result.to_dict(), default=str))

    def outcome(self, outcome: gl.GatewayLifecycleOutcome) -> None:
        self.diagnostic(outcome.diagnostic)
        self.texts += [outcome.describe(), json.dumps(outcome.to_dict(), default=str)]

    def text(self, text: str) -> None:
        self.texts.append(text)
        self.codes.update(re.findall(r"\b([a-z_]+) \[(?:configuration|gateway_health)\]", text))

    def resolve(self, **kwargs: Any) -> BootstrapResult | None:
        try:
            result = resolve_provider_bootstrap(**kwargs)
        except BootstrapError as exc:
            self.refusal(exc)
            return None
        self.result(result)
        return result


def _valid_config(fakes: Fakes, *, gateway_port: int = 20999) -> str:
    return (
        "providers:\n"
        "  omniroute:\n"
        f"    base_url: http://127.0.0.1:{gateway_port}/v1\n"
        "    api_key_env: OMNIROUTE_API_KEY\n"
        "  envprov:\n"
        "    base_url: http://127.0.0.1:21000/v1\n"
        f"    api_key_env: {ENV_ONLY_KEY}\n"
        "  storeprov:\n"
        "    base_url: http://127.0.0.1:21001/v1\n"
        f"    api_key_env: {STORE_ONLY_KEY}\n"
        "  unsetprov:\n"
        "    base_url: http://127.0.0.1:21002/v1\n"
        f"    api_key_env: {UNSET_KEY}\n"
        # A start command can carry the key on its command line.
        f"gateway_start_command: [fake-gateway, serve, --api-key, {fakes.gateway_key}]\n"
        "gateway_ready_timeout_s: 5\n"
    )


# --- resolver paths -----------------------------------------------------------


def _valid_config_with_shadowed_and_missing_values(fakes: Fakes) -> Rendered:
    rendered = Rendered()
    env = {
        **fakes.env,
        # Malformed lower-precedence values, shadowed by the config file.
        "VERDICT_GATEWAY_START_COMMAND": f"fake-gateway --api-key '{fakes.gateway_key}",
        "VERDICT_GATEWAY_READY_TIMEOUT_S": "not-a-number",
    }
    rendered.resolve(
        env=env,
        config_path=fakes.config("valid", _valid_config(fakes)),
        credential_store_env=fakes.store,
    )
    return rendered


def _gateway_key_unset_everywhere(fakes: Fakes) -> Rendered:
    rendered = Rendered()
    store = {k: v for k, v in fakes.store.items() if k not in GATEWAY_KEY_ENV_NAMES}
    rendered.resolve(
        env=fakes.without(*GATEWAY_KEY_ENV_NAMES),
        config_path=fakes.tmp_path / "absent.yaml",
        credential_store_env=store,
    )
    return rendered


def _no_provider_configuration(fakes: Fakes) -> Rendered:
    rendered = Rendered()
    env = fakes.without("OMNIROUTE_BASE_URL")
    absent = fakes.tmp_path / "absent.yaml"
    rendered.resolve(env=env, config_path=absent, credential_store_env=fakes.store)
    rendered.resolve(
        env=env, config_path=absent, credential_store_env=fakes.store, allow_default_providers=True
    )
    rendered.resolve(
        env=env,
        config_path=absent,
        credential_store_env=fakes.store,
        allow_default_providers=True,
        require_authoritative=True,
    )
    return rendered


def _malformed_config_files(fakes: Fakes) -> Rendered:
    rendered = Rendered()
    secret = fakes.gateway_key
    bodies = {
        "unparsable": f"primary_model: x\napi_key: {secret}\nproviders: [{secret}\n",
        "not-mapping": f"- {secret}\n",
        "providers-not-mapping": f"providers: {secret}\n",
        "entry-not-mapping": f"providers:\n  omniroute: {secret}\n",
        "base-url-missing": (
            f"providers:\n  omniroute:\n    api_key_env: OMNIROUTE_API_KEY\n    note: {secret}\n"
        ),
        "base-url-invalid": (
            f"providers:\n  omniroute:\n    base_url: ftp://fakeuser:{secret}@127.0.0.1/v1\n"
        ),
        "gateway-url-invalid": (
            "providers:\n  omniroute:\n    base_url: http://127.0.0.1:20999/v1\n"
            f"gateway_url: ftp://fakeuser:{secret}@127.0.0.1:20128\n"
        ),
        "start-command-wrong-type": (
            "providers:\n  omniroute:\n    base_url: http://127.0.0.1:20999/v1\n"
            "gateway_start_command: 5\n"
        ),
    }
    env = fakes.without("OMNIROUTE_BASE_URL")
    for name, body in bodies.items():
        rendered.resolve(
            env=env, config_path=fakes.config(name, body), credential_store_env=fakes.store
        )
    if os.geteuid() != 0:  # root reads a 000 file, so this path is unreachable as root
        unreadable = fakes.config("unreadable", f"api_key: {secret}\n")
        unreadable.chmod(0)
        rendered.resolve(env=env, config_path=unreadable, credential_store_env=fakes.store)
    return rendered


def _malformed_environment(fakes: Fakes) -> Rendered:
    rendered = Rendered()
    absent = fakes.tmp_path / "absent.yaml"
    rendered.resolve(
        env={
            **fakes.env,
            "OMNIROUTE_BASE_URL": f"ftp://fakeuser:{fakes.gateway_key}@127.0.0.1:20128",
        },
        config_path=absent,
        credential_store_env=fakes.store,
    )
    rendered.resolve(
        env={
            **fakes.env,
            "VERDICT_GATEWAY_START_COMMAND": f"fake-gateway --api-key '{fakes.gateway_key}",
        },
        config_path=absent,
        credential_store_env=fakes.store,
    )
    rendered.resolve(
        env={**fakes.env, "VERDICT_GATEWAY_READY_TIMEOUT_S": "0"},
        config_path=absent,
        credential_store_env=fakes.store,
    )
    return rendered


# --- gateway health and lifecycle paths ----------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.reads = 0

    def __call__(self) -> float:
        self.reads += 1
        assert self.reads < 5000, "the wait is not bounded"
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.01)


class _Probe:
    def __init__(self, health: gl.GatewayHealth) -> None:
        self.health = health

    def __call__(self, url: str) -> gl.GatewayHealth:
        return self.health


class _Process:
    def __init__(self, exit_code: int | None) -> None:
        self._exit_code = exit_code
        self.pid = 4242

    def poll(self) -> int | None:
        return self._exit_code

    def terminate(self) -> None:
        self._exit_code = -15

    def kill(self) -> None:
        self._exit_code = -9


class _Launcher:
    def __init__(self, *, exit_code: int | None = None, fail: bool = False) -> None:
        self.exit_code = exit_code
        self.fail = fail

    def launch(self, argv: tuple[str, ...], *, cwd: Path) -> _Process:
        if self.fail:
            # A launcher error can quote the argv it was handed.
            raise FileNotFoundError(f"[Errno 2] No such file or directory: {argv!r}")
        return _Process(self.exit_code)


CLOSED = gl.GatewayHealth(reachable=False, responded=False, detail="connection refused")
SICK = gl.GatewayHealth(reachable=False, responded=True, detail="HTTP 503")
REFUSED_KEY = gl.GatewayHealth(reachable=False, responded=True, detail="HTTP 401", auth_failed=True)


def _gateway_health_and_lifecycle(fakes: Fakes) -> Rendered:
    rendered = Rendered()
    result = rendered.resolve(
        env=fakes.env,
        config_path=fakes.config("valid", _valid_config(fakes)),
        credential_store_env=fakes.store,
    )
    assert result is not None and result.gateway_required

    def raising_probe(url: str) -> GatewayProbeResult:
        # A transport error quotes the URL it was given, userinfo included.
        raise OSError(f"cannot connect to {url}")

    rendered.diagnostic(
        verify_gateway_reachable(result, probe=lambda url: GatewayProbeResult(False, "refused"))
    )
    rendered.diagnostic(verify_gateway_reachable(result, probe=raising_probe))
    rendered.diagnostic(
        verify_gateway_reachable(dataclasses.replace(result, gateway_url=None), probe=print)
    )

    def ensure(
        target: BootstrapResult, health: gl.GatewayHealth, launcher: _Launcher
    ) -> gl.GatewayLifecycleOutcome:
        clock = _Clock()
        return gl.ensure_gateway_ready(
            target,
            probe=_Probe(health),
            launcher=launcher,
            clock=clock,
            sleep=clock.sleep,
            state_dir=fakes.tmp_path / "gateway-state",
        )

    remote = dataclasses.replace(
        result, gateway_url=f"http://fakeuser:{fakes.gateway_key}@203.0.113.9:20128"
    )
    cases = [
        (result, CLOSED, _Launcher(fail=True)),
        (result, CLOSED, _Launcher(exit_code=1)),
        (result, CLOSED, _Launcher(exit_code=None)),
        (result, SICK, _Launcher()),
        (result, REFUSED_KEY, _Launcher()),
        (remote, CLOSED, _Launcher()),
        (dataclasses.replace(result, gateway_start_command=None), CLOSED, _Launcher()),
        (dataclasses.replace(result, gateway_url=None), CLOSED, _Launcher()),
    ]
    for target, health, launcher in cases:
        rendered.outcome(ensure(target, health, launcher))
    rendered.outcome(gl.inspect_gateway(result, probe=_Probe(CLOSED)))
    clock = _Clock()
    try:
        gl.require_gateway_ready(
            result,
            probe=_Probe(CLOSED),
            launcher=_Launcher(fail=True),
            clock=clock,
            sleep=clock.sleep,
            state_dir=fakes.tmp_path / "gateway-state",
        )
    except BootstrapError as exc:
        rendered.refusal(exc)
    return rendered


# --- public entry points that read the real environment and store ---------------


def _export(
    fakes: Fakes,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    unset: tuple[str, ...] = GATEWAY_KEY_ENV_NAMES,
) -> None:
    """Export ``env`` and write the on-disk credential store without ``unset``."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    store_path = fakes.home / ".config" / "verdict" / "credentials.env"
    store = {k: v for k, v in fakes.store.items() if k not in unset}
    store_path.write_text("".join(f"{k}={v}\n" for k, v in sorted(store.items())), "utf-8")
    store_path.chmod(0o600)


def _routing_config(fakes: Fakes) -> None:
    (fakes.home / ".config" / "verdict" / "verdict.yaml").write_text(
        "primary_model: vendor/cfg-model\n" + _valid_config(fakes, gateway_port=20128),
        encoding="utf-8",
    )


class _StopBeforeRoutingError(Exception):
    """Raised in place of routing. The bootstrap emitters have run by then."""


def _verdict_route(fakes: Fakes, monkeypatch: pytest.MonkeyPatch) -> None:
    """``verdict route`` through ``cli.main``, stopped before any routing decision.

    ``cli.main`` copies the on-disk store into ``os.environ`` itself, outside
    ``monkeypatch``. Each stored name is recorded as unset first, so teardown
    removes whatever the CLI exported, and its provenance record stays local.
    """
    import verdict.actions.registry as registry
    import verdict.credentials_store as credentials_store
    from verdict import cli

    for name in fakes.store:
        if name not in os.environ:
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    monkeypatch.setattr(credentials_store, "_STORE_INJECTED", {})

    def stop(*_: object, **__: object) -> None:
        raise _StopBeforeRoutingError

    monkeypatch.setattr(registry, "run_action", stop)
    monkeypatch.setattr(
        cli.sys, "argv", ["verdict", "--skip-setup", "route", "hello", "--allow-offline", "--terse"]
    )
    cli.main()


def _cli_route_and_library_gate(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Rendered:
    from verdict.gate import Gate

    rendered = Rendered()
    log_path = str(fakes.tmp_path / "decisions.jsonl")
    # The gateway key is only in the owner-only key file and Prime's auth.json.
    env = fakes.without(*GATEWAY_KEY_ENV_NAMES)
    env["OMNIROUTE_BASE_URL"] = "http://127.0.0.1:20128"
    _export(fakes, monkeypatch, env)
    _routing_config(fakes)
    key_file = fakes.home / ".config" / "verdict" / GATEWAY_KEY_FILENAME
    key_file.write_text(_fake("keyfile", 0), "utf-8")
    key_file.chmod(0o600)
    auth = fakes.home / ".prime" / "agent" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(json.dumps({"omniroute": {"key": _fake("primeauth", 0)}}), "utf-8")
    auth.chmod(0o600)
    fakes.values.update((_fake("keyfile", 0), _fake("primeauth", 0)))

    with pytest.raises(_StopBeforeRoutingError):
        _verdict_route(fakes, monkeypatch)
    Gate(allow_offline=True, log_path=log_path)
    rendered.text(capsys.readouterr().err)

    # A malformed config refuses on both paths.
    (fakes.home / ".config" / "verdict" / "verdict.yaml").write_text(
        f"providers: [{fakes.gateway_key}\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit) as route_exit:
        _verdict_route(fakes, monkeypatch)
    assert route_exit.value.code == 1
    with pytest.raises(BootstrapError) as excinfo:
        Gate(allow_offline=True, log_path=log_path)
    rendered.refusal(excinfo.value)
    rendered.text(capsys.readouterr().err)
    return rendered


def _serve_startup(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Rendered:
    rendered = Rendered()
    # Missing gateway key and management token; every other name is set. Only the
    # gateway URL is exported among the URL names, so serve boots offline.
    missing = (*GATEWAY_KEY_ENV_NAMES, "OMNIROUTE_MANAGEMENT_TOKEN")
    env = fakes.without(*missing, *URL_NAMES)
    env["OMNIROUTE_BASE_URL"] = "http://localhost:20128"
    env["LLMGATE_PRIMARY"] = "vendor/env-model"
    _export(fakes, monkeypatch, env, unset=missing)
    monkeypatch.delenv("LLMGATE_ALLOW_ANONYMOUS", raising=False)
    _routing_config(fakes)
    monkeypatch.setattr(api, "_build_proxy", lambda: UpstreamProxy("https://api.example.test/v1"))

    with TestClient(api.app):
        pass
    rendered.text(capsys.readouterr().err)
    # With no gateway URL either, the upstream falls back to the built-in default.
    monkeypatch.delenv("OMNIROUTE_BASE_URL")
    report = api.server_bootstrap_diagnostics()
    rendered.text(json.dumps(report, default=str))
    rendered.codes.update(entry["code"] for entry in report["diagnostics"])
    return rendered


def _supervisor_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "prime_supervisor_no_values", ROOT / "scripts" / "prime_supervisor.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prime_supervisor(
    fakes: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Rendered:
    """``main --select-only`` with the real production factory, which refuses for
    missing providers. ``_live_admission_gateway`` stays on its private seam: it
    runs only inside controller selection after a production bundle exists, so
    no public entry reaches it offline (see the emitter test for the same note).
    """
    from verdict.admission import AdmissionUnavailableError

    rendered = Rendered()
    _export(fakes, monkeypatch, fakes.without("OMNIROUTE_BASE_URL"))
    supervisor = _supervisor_module()
    repo, state = fakes.tmp_path / "repo", fakes.tmp_path / "state"
    repo.mkdir()
    state.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    identity = ["-c", "user.name=T", "-c", "user.email=t@e.invalid"]
    commit = ["git", "-C", str(repo), *identity, "commit", "--allow-empty", "-qm", "base"]
    subprocess.run(commit, check=True)

    def never_launch(*_: object, **__: object) -> None:
        raise AssertionError("select-only must never launch Prime")

    monkeypatch.setattr(supervisor, "run_attempt", never_launch)
    # Worker and reviewer routes come from live OmniRoute availability; they are
    # not bootstrap emitters, so they are answered offline here.
    monkeypatch.setattr(
        supervisor,
        "_resolve_subagent_report_routes",
        lambda roles=("worker", "reviewer"): {role: {"error": "not_under_test"} for role in roles},
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *("prime_supervisor.py", "--repo", str(repo), "--state-dir", str(state)),
            *("--prime", "prime-agent", "--select-only"),
        ],
    )
    assert supervisor.main() == 2
    rendered.text(capsys.readouterr().out)

    with pytest.raises(AdmissionUnavailableError) as admission:
        supervisor._live_admission_gateway()
    rendered.text(f"{admission.value.detail}\n{admission.value}")
    return rendered


EntryPoint = Callable[[Fakes, pytest.MonkeyPatch, pytest.CaptureFixture[str]], Rendered]

PATHS: dict[str, tuple[Callable[..., Rendered], set[str]]] = {
    "resolver-valid-config": (
        _valid_config_with_shadowed_and_missing_values,
        {"credential_env_missing", "default_primary_model", "precedence_conflict"},
    ),
    "resolver-gateway-key-unset": (
        _gateway_key_unset_everywhere,
        {"config_file_missing", "credential_env_missing", "default_primary_model"},
    ),
    "resolver-no-providers": (
        _no_provider_configuration,
        {"no_provider_configuration", "default_provider_fallback", "default_providers_forbidden"},
    ),
    "resolver-malformed-config": (
        _malformed_config_files,
        {
            "config_file_unparsable",
            "config_file_not_mapping",
            "config_providers_malformed",
            "provider_entry_malformed",
            "provider_base_url_missing",
            "provider_base_url_invalid",
            "gateway_start_command_malformed",
        }
        | ({"config_file_unreadable"} if os.geteuid() != 0 else set()),
    ),
    "resolver-malformed-environment": (
        _malformed_environment,
        {
            "provider_base_url_invalid",
            "gateway_start_command_malformed",
            "gateway_ready_timeout_invalid",
        },
    ),
    "gateway-health-and-lifecycle": (
        _gateway_health_and_lifecycle,
        {
            "gateway_unreachable",
            "gateway_required_but_absent",
            "gateway_start_failed",
            "gateway_process_exited",
            "gateway_ready_timeout",
            "gateway_unhealthy",
            "gateway_auth_failed",
            "gateway_remote_not_managed",
            "gateway_start_command_missing",
        },
    ),
}

ENTRY_POINTS: dict[str, tuple[EntryPoint, set[str]]] = {
    "cli-route-and-library-gate": (
        _cli_route_and_library_gate,
        {"credential_env_missing", "config_file_unparsable"},
    ),
    "serve-startup": (
        _serve_startup,
        {
            "credential_env_missing",
            "runtime_sources_health_only",
            "precedence_conflict",
            "default_upstream_base_url",
        },
    ),
    "prime-supervisor": (_prime_supervisor, {"no_provider_configuration"}),
}


def _leaks(rendered: Rendered, values: set[str]) -> Iterator[str]:
    for text in rendered.texts:
        for value in sorted(values):
            if value in text:
                yield f"{value!r} in {text[:240]!r}"


def _named_without_a_value(rendered: Rendered, fakes: Fakes) -> set[str]:
    """Credential names in the rendered lines that no source gave a fake value."""
    named = {name for text in rendered.texts for name in CREDENTIAL_NAME.findall(text)}
    return named - fakes.supplied - DELIBERATELY_UNSET


def test_the_name_check_sees_every_config_declared_name(fakes: Fakes) -> None:
    """The name check is not vacuous: the declared names reach rendered lines."""
    rendered = _valid_config_with_shadowed_and_missing_values(fakes)
    named = {name for text in rendered.texts for name in CREDENTIAL_NAME.findall(text)}

    assert {"OMNIROUTE_API_KEY", ENV_ONLY_KEY, STORE_ONLY_KEY, UNSET_KEY} <= named


@pytest.mark.parametrize("name", sorted(PATHS))
def test_no_resolver_or_lifecycle_diagnostic_carries_a_credential_value(
    name: str, fakes: Fakes
) -> None:
    path, expected_codes = PATHS[name]
    rendered = path(fakes)

    assert expected_codes <= rendered.codes, sorted(expected_codes - rendered.codes)
    assert list(_leaks(rendered, fakes.values)) == []
    assert _named_without_a_value(rendered, fakes) == set()


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_no_public_entry_point_prints_a_credential_value(
    name: str, fakes: Fakes, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    entry_point, expected_codes = ENTRY_POINTS[name]
    rendered = entry_point(fakes, monkeypatch, capsys)

    assert expected_codes <= rendered.codes, sorted(expected_codes - rendered.codes)
    assert list(_leaks(rendered, fakes.values)) == []
    assert _named_without_a_value(rendered, fakes) == set()


def test_fake_values_go_exactly_where_the_docstring_says(fakes: Fakes) -> None:
    """The name lists come from the code, so a new credential is covered automatically."""
    assert set(GATEWAY_KEY_ENV_NAMES) <= set(SECRET_NAMES)
    assert "OMNIROUTE_MANAGEMENT_TOKEN" in SECRET_NAMES
    assert "OMNIROUTE_BASE_URL" in URL_NAMES
    assert {name for name in fakes.env if name in KNOWN_NAMES} == set(KNOWN_NAMES)
    for name in SECRET_NAMES:
        assert fakes.env[name] != fakes.store[name], name
    assert STORE_ONLY_KEY in fakes.store and STORE_ONLY_KEY not in fakes.env
    assert UNSET_KEY not in fakes.supplied
    assert all(name not in fakes.store for name in URL_NAMES)
