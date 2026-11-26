"""Test environment isolation - remove operator env leaks."""

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.helpers import home_guard

# Registers the real-home write guard (audit hook + per-test failure); see
# tests/helpers/home_guard.py and tests/test_home_isolation.py.
from tests.helpers.home_guard import pytest_configure  # noqa: F401


@pytest.fixture(autouse=True)
def _isolate_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove LLMGATE_AUTH_TOKEN to prevent operator shell leakage into test suite."""
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)


_CREDENTIAL_PREFIXES = (
    "OMNIROUTE_",
    "LLMGATE_",
    "VERDICT_OMNIROUTE_",
    "TYPESAFE_",
    "OPENROUTER_",
    "OPENAI_",
    "ANTHROPIC_",
)


@pytest.fixture(autouse=True)
def _isolate_provider_credentials(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delete provider credential env vars so tests never leak into live services.

    Tests marked ``@pytest.mark.integration`` are exempt.
    """
    if request.node.get_closest_marker("integration"):
        return
    for key in list(os.environ):
        if any(key.startswith(prefix) for prefix in _CREDENTIAL_PREFIXES):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _isolate_verdict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep controller/test env isolated unless a test sets it explicitly, and
    give the API an explicit in-memory receipts store (production code no longer
    detects pytest via PYTEST_CURRENT_TEST)."""
    monkeypatch.delenv("VERDICT_ALLOW_UNVERIFIED_DEV", raising=False)
    for key in (
        "CODEX_HOME",
        "ORCA_CODEX_HOME",
        "VERDICT_SUPERVISED_RUN_DIR",
        "VERDICT_CONTROLLER_TOKEN",
        "VERDICT_CONTROLLER_DEADLINE",
        "VERDICT_CONTROLLER_LAUNCH",
        "VERDICT_CONTROLLER_OPERATION",
        "VERDICT_CONTROLLER_CGROUP_ROOT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VERDICT_RECEIPTS_DB", ":memory:")


_ORIGINAL_TESTCLIENT_INIT = TestClient.__init__


def _loopback_testclient_init(self: TestClient, *args: object, **kwargs: object) -> None:
    # Starlette's default peer is ("testclient", 50000), which is not an IP.
    # Production auth treats only real loopback IPs as loopback, so default the
    # synthetic peer to 127.0.0.1. Tests that pass client=... keep their value.
    kwargs.setdefault("client", ("127.0.0.1", 50000))
    _ORIGINAL_TESTCLIENT_INIT(self, *args, **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _loopback_testclient_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make TestClient default to a real loopback peer address."""
    monkeypatch.setattr(TestClient, "__init__", _loopback_testclient_init)


class LiveNetworkAttempted(BaseException):
    """Raised when a guarded test tries to open a real connection.

    Deliberately a ``BaseException``: the gateway probe path catches ``Exception``
    and reports it as "did not answer", which would turn an accidental live call
    into a silently passing test. A ``BaseException`` escapes that handler, so the
    test fails loudly and names the address it tried to reach.
    """


@pytest.fixture
def no_gateway_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any real outbound connection an immediate, loud failure.

    Blocks the two ways a gateway probe could reach the network: ``httpx``'s
    request entry points, and the socket layer underneath them. ASGI in-process
    clients (``TestClient``) do not open sockets, so they are unaffected.
    """
    import socket

    import httpx

    def refuse(target: object) -> None:
        raise LiveNetworkAttempted(
            f"this test must not open a connection, but it tried to reach {target!r}"
        )

    def refuse_request(*args: object, **kwargs: object) -> None:
        url = kwargs.get("url")
        if url is None and len(args) > 1:
            url = args[1]
        refuse(url)

    for name in ("get", "post", "head", "request", "stream"):
        monkeypatch.setattr(httpx, name, refuse_request, raising=False)
    monkeypatch.setattr(httpx.Client, "send", refuse_request, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", refuse_request, raising=False)
    monkeypatch.setattr(
        socket.socket, "connect", lambda self, address: refuse(address), raising=False
    )
    monkeypatch.setattr(
        socket.socket, "connect_ex", lambda self, address: refuse(address), raising=False
    )
    monkeypatch.setattr(
        socket, "create_connection", lambda address, *a, **k: refuse(address), raising=False
    )


@pytest.fixture(autouse=True)
def _isolate_subscription_ledger():
    from verdict.cost_ledger import _subscription_reserved, subscription_budgets

    subscription_budgets.clear()
    _subscription_reserved.clear()
    yield
    subscription_budgets.clear()
    _subscription_reserved.clear()


@pytest.fixture(autouse=True, scope="session")
def _repo_instructions_untouched() -> Any:
    """Fail the run if tests change repository-root agent instructions."""
    root = Path(__file__).resolve().parents[1]
    paths = [root / name for name in ("CLAUDE.md", "AGENTS.md")]

    def hashes() -> dict[Path, bytes | None]:
        return {
            path: hashlib.sha256(path.read_bytes()).digest() if path.exists() else None
            for path in paths
        }

    before = hashes()
    yield
    after = hashes()
    changed = [path.name for path in paths if before[path] != after[path]]
    assert not changed, f"tests modified repository instructions: {', '.join(changed)}"


_REAL_PRIME_MODELS = Path(os.path.expanduser("~")) / ".prime" / "agent" / "models.json"


def _fingerprint(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


@pytest.fixture(autouse=True)
def _isolate_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests never read or write the operator's real home (~/.prime, ~/.verdict).

    Code that defaults to ``Path.home()`` (for example Prime visibility refresh
    writing ``~/.prime/agent/models.json``) otherwise rewrites the operator's
    live Prime registry with fixture rows.
    """
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in ("PRIME_AGENT_HOME", "PRIME_HOME", "PRIME_AGENT_CODING_AGENT_DIR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _termination_signal_handlers_do_not_outlive_the_test() -> Any:
    """Whatever a test installs for SIGTERM, SIGINT or SIGHUP is gone when the test ends.

    These handlers belong to the process, and an xdist worker runs many files in one process.
    ``scripts/prime_supervisor.py``'s ``main()`` installs a SIGTERM and a SIGHUP handler and
    never gives them back, so every test that calls it in process (several files do) left the
    next file in the worker with a handler that was not the default; a test that asks "what
    does this run do to SIGTERM" then saw somebody else's.  Pinned by
    ``tests/test_signal_handler_isolation.py``.
    """
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        yield  # only the main thread can set a handler
        return
    names = ("SIGTERM", "SIGINT", "SIGHUP")
    before = {
        number: signal.getsignal(number)
        for number in (getattr(signal, name) for name in names if hasattr(signal, name))
    }
    yield
    for number, handler in before.items():
        # ``None`` is a handler installed from C: it cannot be set back from Python.
        if handler is not None and signal.getsignal(number) != handler:
            signal.signal(number, handler)


@pytest.fixture(autouse=True)
def _isolate_systemd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor timer probes never ask the operator's real systemd user manager."""
    import subprocess

    def not_installed(args: list[str]) -> "subprocess.CompletedProcess[str]":
        return subprocess.CompletedProcess(args, 0, stdout="LoadState=not-found\n", stderr="")

    monkeypatch.setattr("verdict.metadata_service._run_show", not_installed)
    monkeypatch.setattr("verdict.identify_service._run_show", not_installed)
    monkeypatch.setattr("verdict.prove_at_rest_service._run_systemctl_show", not_installed)
    monkeypatch.setattr("verdict.doctor_diagnostics._run_systemctl_show_props", not_installed)


# Single source of truth lives in tests/helpers/home_guard.py so the isolation
# test can check the list against the env names verdict/ actually reads.
_HOST_STATE_ENV = home_guard.HOST_STATE_ENV


@pytest.fixture(autouse=True)
def _isolate_host_state_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop exported state paths so every default resolves under the isolated HOME."""
    for key in _HOST_STATE_ENV:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _state_paths_resolve_under_the_per_test_home(
    _isolate_home: None, _isolate_host_state_env: None
) -> None:
    """Fail setup if a derived Verdict path would land outside the per-test home.

    Depends on both isolation fixtures so it runs after them. A test that
    re-points HOME or VERDICT_HOME itself does so later, inside its own body.
    """
    from verdict.paths import default_decision_log_path, verdict_home

    home = Path(os.environ["HOME"]).resolve()
    assert str(home) not in home_guard.REAL_HOMES, f"HOME is the real home: {home}"
    for label, resolved in (
        ("verdict_home()", verdict_home()),
        ("default_decision_log_path()", Path(default_decision_log_path())),
    ):
        assert resolved.resolve().is_relative_to(home), (
            f"{label} resolves to {resolved}, outside the per-test home {home}"
        )


@pytest.fixture(autouse=True)
def _isolate_systemctl_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit installers never run the operator's real ``systemctl --user``.

    Tests that check the issued commands pass ``run=`` explicitly (existing pattern).
    """

    def refuse(args: list[str]) -> None:
        raise AssertionError(f"test tried to run real systemctl: {args}")

    # ``run=_systemctl`` is a keyword-only default bound at import time, so
    # patching the module attribute would not reach it; patch the default.
    from verdict import (
        acceptance_service,
        census_passports_service,
        docs_refresh_service,
        identify_service,
        metadata_service,
        prove_at_rest_service,
    )

    for installer in (
        acceptance_service.manage_service,
        census_passports_service.manage_census_service,
        docs_refresh_service.manage_service,
        identify_service.manage_service,
        metadata_service.manage_service,
        prove_at_rest_service.manage_service,
    ):
        monkeypatch.setitem(installer.__kwdefaults__, "run", refuse)


@pytest.fixture(autouse=True, scope="session")
def _real_prime_registry_untouched() -> Any:
    """Fail the run if any test modified the real Prime model registry."""
    before = _fingerprint(_REAL_PRIME_MODELS)
    yield
    after = _fingerprint(_REAL_PRIME_MODELS)
    if before != after and _changed_by_sync_timer(after):
        return
    assert before == after, f"a test modified the real {_REAL_PRIME_MODELS}"


def _changed_by_sync_timer(after: tuple[int, int] | None) -> bool:
    """True when the change is the operator's verdict-prime-sync timer run.

    That timer rewrites the real registry every 15 minutes from another
    process, and every write leaves models.json.verdict-sync-<stamp>.bak
    alongside it within seconds. Tests never write that backup name (HOME is
    isolated), so a matching fresh backup identifies the timer, not a test.
    """
    if after is None:
        return False
    from datetime import datetime, timezone

    written_at = after[0] / 1e9
    for backup in _REAL_PRIME_MODELS.parent.glob("models.json.verdict-sync-*.bak"):
        # The stamp in the name is when the sync ran; the file's own mtime is
        # the PREVIOUS registry's (the backup preserves it), so use the name.
        stamp = backup.name[len("models.json.verdict-sync-") : -len(".bak")]
        try:
            ran_at = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if abs(ran_at.timestamp() - written_at) <= 10:
            return True
    return False


@pytest.fixture
def billing_overlay_subscription() -> Callable[[list[tuple[str, str]]], None]:
    """Return a writer that declares (provider, auth_type) pairs as SUBSCRIPTION.

    CP2 removed the oauth → SUBSCRIPTION rule (design.md CP2 §2.2, 2026-11-19).
    Tests whose fake oauth providers must classify as SUBSCRIPTION call this
    fixture to write the operator overlay that the billing lookup reads.

    Example::

        @pytest.fixture(autouse=True)
        def _my_subscription_overlay(billing_overlay_subscription):
            billing_overlay_subscription([("cc", "oauth"), ("kr", "oauth")])

    The overlay is written under the HOME that ``_isolate_home`` already
    isolated, so it never touches the real ``~/.verdict``.  VERDICT_HOME is
    cleared by ``_isolate_host_state_env`` (autouse), so ``verdict_home()``
    resolves under HOME.  The ``_overlay_cache`` in ``connection_billing`` is
    keyed on ``(path, mtime)``; the unique tmp HOME per test guarantees a cache
    miss, so each test sees only its own overlay.
    """

    def write(providers: list[tuple[str, str]]) -> None:
        from verdict.paths import verdict_home

        home = verdict_home()
        home.mkdir(parents=True, exist_ok=True)
        path = home / "connection_billing_overlay.json"
        entries = [
            {"provider": p, "authType": a, "billing": "subscription", "overage": "hard-stop"}
            for (p, a) in providers
        ]
        path.write_text(json.dumps({"entries": entries}))

    return write
