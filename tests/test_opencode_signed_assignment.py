"""The OpenCode executor signs one assignment per attempt and gives it to the worker (#1325).

The assignment (route, run id, node id, issued-at, expiry) is the worker's only credential for
``verdict serve``.  The serve token stays in the orchestrator's process, for the readiness check.
These tests drive the executor with fake workers (scripts that record what they are given); the
executor-to-serve path through the real app is in ``test_opencode_assignment_serve_path.py``.
Nothing here reads a real home, key or database, or contacts anything but loopback.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import verdict.orchestration.executor_opencode as executor_module
from tests import worker_assignment_helpers as sign
from verdict import worker_assignment as wa
from verdict.orchestration.executor_opencode import (
    ASSIGNMENT_MARGIN_SECONDS,
    OpenCodeExecutor,
    _assignment_lifetime,
)
from verdict.orchestration.recovery import FailureIntelligence
from verdict.receipt_store import ReceiptStore

ROUTE = "cc/claude-sonnet-5"
RUN_ID = "run-20261125"
NODE_ID = "implement-1"
KEY = bytes(range(32))
SERVE_TOKEN = "serve-token-UNIQUE-77aa31"
FIXTURES = Path(__file__).parent / "fixtures" / "opencode_stdout"


class Calls:
    """What the pre-dispatch checks were asked."""

    def __init__(self) -> None:
        self.serve: list[tuple[str, str]] = []


@pytest.fixture(autouse=True)
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """The binary runs; serve accepts the serve token and the call is recorded."""
    seen = Calls()

    def check_serve(origin: str, token: str, timeout: float) -> str | None:
        seen.serve.append((origin, token))
        return None

    monkeypatch.setattr(executor_module, "_check_binary", lambda *a, **k: None)
    monkeypatch.setattr(executor_module, "_check_serve", check_serve)
    return seen


def fake_worker(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-opencode"
    script.write_text(
        "#!/usr/bin/env python3\nimport sys, json, os, time\n" + textwrap.dedent(body)
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def executor(binary: str, **overrides: Any) -> OpenCodeExecutor:
    args: dict[str, Any] = {
        "opencode_bin": binary,
        "serve_token_provider": lambda: SERVE_TOKEN,
        "key_provider": lambda: KEY,
        "receipts_db_provider": lambda: None,
    }
    args.update(overrides)
    return OpenCodeExecutor(**args)


def go(
    ex: OpenCodeExecutor,
    cwd: Path,
    *,
    timeout: float = 10.0,
    run_id: Any = RUN_ID,
    node_id: Any = NODE_ID,
) -> Any:
    return asyncio.run(
        ex.run(
            "task", route_id=ROUTE, cwd=cwd, timeout_seconds=timeout, run_id=run_id, node_id=node_id
        )
    )


def workdir(tmp_path: Path) -> Path:
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    return cwd


def recording_worker(tmp_path: Path) -> tuple[str, Path]:
    """A worker that stores the credential it was handed and exits 0."""
    seen = tmp_path / "credential.txt"
    return (
        fake_worker(
            tmp_path,
            f"open({str(seen)!r}, 'w').write(os.environ.get('VERDICT_WORKER_API_KEY', ''))\n"
            "sys.exit(0)\n",
        ),
        seen,
    )


def marker_worker(tmp_path: Path) -> tuple[str, Path]:
    """A worker that leaves a marker when it is launched at all."""
    ran = tmp_path / "worker-ran.txt"
    return (fake_worker(tmp_path, f"open({str(ran)!r}, 'w').write('ran')\nsys.exit(0)\n"), ran)


# --- the lifetime ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeout", "lifetime"),
    [(1.0, 61), (10, 70), (100.2, 161), (885.0, 945), (900, 960), (1740.0, 1800)],
)
def test_the_lifetime_is_the_attempt_timeout_rounded_up_plus_the_margin(
    timeout: float, lifetime: int
) -> None:
    assert ASSIGNMENT_MARGIN_SECONDS == 60
    assert _assignment_lifetime(timeout) == lifetime
    assert lifetime == math.ceil(timeout) + ASSIGNMENT_MARGIN_SECONDS


def test_the_attempt_the_runtime_allows_fits_the_format_with_room_to_spare() -> None:
    # The runtime caps an attempt at 900 s and the executor is given 10% (at most 15 s) less.
    assert _assignment_lifetime(900.0) <= wa.MAX_LIFETIME_SECONDS - 800


def test_the_signed_assignment_carries_exactly_that_lifetime_dated_now(tmp_path: Path) -> None:
    binary, seen = recording_worker(tmp_path)
    now = datetime(2026, 11, 25, 9, 30, 15, 700_000, tzinfo=timezone.utc)
    terminal = go(executor(binary, clock=lambda: now), workdir(tmp_path), timeout=100.2)
    assert terminal.ok, terminal
    fields = sign.decode_payload(seen.read_text())
    assert fields == {
        "route_id": ROUTE,
        "run_id": RUN_ID,
        "node_id": NODE_ID,
        "iat": int(now.timestamp()),
        "exp": int(now.timestamp()) + 101 + ASSIGNMENT_MARGIN_SECONDS,
    }


@pytest.mark.parametrize("timeout", [1740.001, 1741, 2000, 3600.0, 1e9])
def test_a_timeout_that_would_need_more_than_1800_seconds_fails_closed_before_launch(
    tmp_path: Path, calls: Calls, timeout: float
) -> None:
    binary, ran = marker_worker(tmp_path)
    keys: list[int] = []
    cwd = workdir(tmp_path)
    terminal = go(
        executor(binary, key_provider=lambda: keys.append(1) or KEY), cwd, timeout=timeout
    )
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration:"), terminal.error
    assert "1800" in terminal.error and "lower the attempt timeout" in terminal.error
    assert not ran.exists(), "the worker was launched"
    assert calls.serve == [], "serve was contacted for an attempt that cannot be signed"
    assert keys == [], "the key was read for an attempt that cannot be signed"
    assert list(cwd.iterdir()) == []
    assert terminal.worker_assignment is None


@pytest.mark.parametrize("timeout", [0, -1, -0.5, math.nan, math.inf, -math.inf, True, None, "10"])
def test_a_timeout_that_is_not_a_positive_finite_number_is_a_configuration_block(
    tmp_path: Path, calls: Calls, timeout: Any
) -> None:
    binary, ran = marker_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path), timeout=timeout)
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration:"), terminal.error
    assert "positive, finite" in terminal.error
    assert not ran.exists() and calls.serve == []
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action) == ("harness_configuration", "BLOCK")


# --- the signing key and the ids ---------------------------------------------------------


def _loader_for(home: Path) -> Any:
    return lambda: wa.load_assignment_key(home)


def _unreadable_key(home: Path) -> None:
    path = wa.assignment_key_path(home)
    path.parent.mkdir(parents=True)
    path.write_text(sign.b64url(KEY))
    path.chmod(0)


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda home: None, id="missing"),
        pytest.param(
            _unreadable_key,
            id="unreadable",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root can read a mode 0 file, so nothing is unreadable"
            ),
        ),
        pytest.param(
            lambda home: (
                wa.assignment_key_path(home).parent.mkdir(parents=True),
                wa.assignment_key_path(home).write_text(sign.b64url(bytes(31))),
            ),
            id="too-short",
        ),
        pytest.param(
            lambda home: (
                wa.assignment_key_path(home).parent.mkdir(parents=True),
                wa.assignment_key_path(home).write_text("not base64url!!"),
            ),
            id="not-a-key",
        ),
        pytest.param(
            lambda home: wa.assignment_key_path(home).mkdir(parents=True), id="a-directory"
        ),
    ],
)
def test_a_missing_or_unreadable_key_blocks_before_launch_and_never_falls_back(
    tmp_path: Path, calls: Calls, setup: Any
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    setup(home)
    binary, ran = marker_worker(tmp_path)
    cwd = workdir(tmp_path)
    try:
        terminal = go(executor(binary, key_provider=_loader_for(home)), cwd)
    finally:
        path = wa.assignment_key_path(home)
        if path.exists() and path.is_file():
            path.chmod(0o600)
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration: the worker assignment signing key")
    assert "key_unavailable" in terminal.error and "verdict setup" in terminal.error
    assert SERVE_TOKEN not in terminal.error and str(home) not in terminal.error
    assert not ran.exists(), "the worker was launched without an assignment"
    assert calls.serve == [], "no request of any kind went to serve"
    assert list(cwd.iterdir()) == []
    assert terminal.worker_assignment is None
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action) == ("harness_configuration", "BLOCK")


def test_a_key_provider_that_raises_is_a_configuration_block_naming_only_the_type(
    tmp_path: Path, calls: Calls
) -> None:
    def boom() -> bytes:
        raise RuntimeError(f"secret detail {SERVE_TOKEN}")

    binary, ran = marker_worker(tmp_path)
    terminal = go(executor(binary, key_provider=boom), workdir(tmp_path))
    assert terminal.error.startswith("harness_configuration:")
    assert "RuntimeError" in terminal.error and "secret detail" not in terminal.error
    assert not ran.exists() and calls.serve == []


@pytest.mark.parametrize("missing", ["run_id", "node_id"])
@pytest.mark.parametrize("value", [None, "", 7, b"run"], ids=["none", "empty", "int", "bytes"])
def test_a_missing_run_id_or_node_id_blocks_before_launch(
    tmp_path: Path, calls: Calls, missing: str, value: Any
) -> None:
    binary, ran = marker_worker(tmp_path)
    cwd = workdir(tmp_path)
    ids = {"run_id": RUN_ID, "node_id": NODE_ID, missing: value}
    terminal = go(executor(binary), cwd, **ids)
    assert terminal.ok is False
    assert terminal.error.startswith("harness_configuration:"), terminal.error
    assert missing.replace("_", " ") in terminal.error
    assert not ran.exists() and calls.serve == [] and list(cwd.iterdir()) == []
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action) == ("harness_configuration", "BLOCK")


@pytest.mark.parametrize("field", ["run_id", "node_id"])
@pytest.mark.parametrize("value", ["x" * 129, "a\nb"], ids=["too-long", "control-character"])
def test_an_id_the_verifier_would_refuse_is_never_signed(
    tmp_path: Path, calls: Calls, field: str, value: str
) -> None:
    binary, ran = marker_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path), **{field: value})
    assert terminal.error.startswith(
        "harness_configuration: the worker assignment cannot be signed"
    )
    assert value not in terminal.error
    assert not ran.exists() and calls.serve == []


def test_the_default_run_without_ids_is_a_block_so_a_planner_call_never_gets_a_credential(
    tmp_path: Path, calls: Calls
) -> None:
    """Planner calls carry no node id.  An executor that is asked to plan (it cannot be: the
    planner wraps it in ``AnswerOnlyExecutor``, which refuses it, see
    ``test_worker_identity_carry.py``) is refused here too, with nothing launched."""
    binary, ran = marker_worker(tmp_path)
    terminal = asyncio.run(
        executor(binary).run("plan", route_id=ROUTE, cwd=workdir(tmp_path), timeout_seconds=10.0)
    )
    assert terminal.error.startswith("harness_configuration: no run id")
    assert not ran.exists() and calls.serve == []


def test_the_serve_token_is_not_the_fallback_when_no_assignment_can_be_signed(
    tmp_path: Path,
) -> None:
    """Every refusal above ends with no worker launched; this one also checks a worker that is
    launched never sees the serve token, even when the key provider fails on the way."""
    seen = tmp_path / "env.json"
    binary = fake_worker(
        tmp_path, f"open({str(seen)!r}, 'w').write(json.dumps(dict(os.environ)))\nsys.exit(0)\n"
    )

    def no_key() -> bytes:
        raise wa.AssignmentError("key_unavailable")

    terminal = go(executor(binary, key_provider=no_key), workdir(tmp_path))
    assert terminal.ok is False and not seen.exists()


# --- the serve token stays in the orchestrator ------------------------------------------


def test_serve_is_checked_with_the_serve_token_and_never_with_the_assignment(
    tmp_path: Path, calls: Calls
) -> None:
    binary, seen = recording_worker(tmp_path)
    terminal = go(executor(binary, serve_base_url="http://127.0.0.1:8123"), workdir(tmp_path))
    assert terminal.ok, terminal
    assert calls.serve == [("http://127.0.0.1:8123", SERVE_TOKEN)]
    assert seen.read_text().startswith("vwa1.")
    assert all(not token.startswith("vwa1.") for _origin, token in calls.serve)


def test_a_serve_token_in_the_process_environment_never_reaches_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LLMGATE_AUTH_TOKEN", "VERDICT_SERVE_TOKEN", "OMNIROUTE_API_KEY"):
        monkeypatch.setenv(name, SERVE_TOKEN)
    seen = tmp_path / "env.json"
    binary = fake_worker(
        tmp_path, f"open({str(seen)!r}, 'w').write(json.dumps(dict(os.environ)))\nsys.exit(0)\n"
    )
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.ok, terminal
    env = json.loads(seen.read_text())
    assert SERVE_TOKEN not in json.dumps(env)
    assert {name for name in env if "TOKEN" in name or name.endswith("_KEY")} == {
        "VERDICT_WORKER_API_KEY"
    }


def test_a_value_already_in_the_workers_key_variable_is_replaced_by_the_assignment(
    tmp_path: Path,
) -> None:
    binary, seen = recording_worker(tmp_path)
    ex = executor(
        binary,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "VERDICT_WORKER_API_KEY": SERVE_TOKEN},
    )
    terminal = go(ex, workdir(tmp_path))
    assert terminal.ok, terminal
    assert seen.read_text().startswith("vwa1.") and SERVE_TOKEN not in seen.read_text()


def test_each_attempt_gets_its_own_assignment_bound_to_its_node(tmp_path: Path) -> None:
    binary, seen = recording_worker(tmp_path)
    clock_values = iter(
        [
            datetime(2026, 11, 25, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=n)
            for n in (0, 5)
        ]
    )
    ex = executor(binary, clock=lambda: next(clock_values))
    cwd = workdir(tmp_path)
    go(ex, cwd, node_id="node-a")
    first = seen.read_text()
    go(ex, cwd, node_id="node-b")
    second = seen.read_text()
    assert first != second
    assert sign.decode_payload(first)["node_id"] == "node-a"
    assert sign.decode_payload(second)["node_id"] == "node-b"


def test_the_assignment_serve_would_verify_is_what_the_worker_gets(tmp_path: Path) -> None:
    binary, seen = recording_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.ok
    verified = wa.verify_assignment(seen.read_text(), KEY, now=datetime.now(timezone.utc))
    assert (verified.route_id, verified.run_id, verified.node_id) == (ROUTE, RUN_ID, NODE_ID)
    # and the terminal names it by the digest serve records, never by the credential
    assert terminal.worker_assignment is not None
    assert terminal.worker_assignment["assignment_digest"] == verified.assignment_digest
    assert seen.read_text() not in json.dumps(terminal.worker_assignment)


# --- serve refusing the worker's assignment ----------------------------------------------


def test_serves_refusal_of_the_assignment_is_a_configuration_block_not_a_provider_denial(
    tmp_path: Path,
) -> None:
    """Captured from the real OpenCode 2.3.2 against a loopback stand-in for serve (see the
    fixtures README).  Read as plain 403 it would cool the provider down for an hour."""
    captured = (FIXTURES / "v2_3_2_error_403_serve_refusal.jsonl").read_text()
    binary = fake_worker(tmp_path, f"sys.stdout.write({captured!r})\nsys.exit(1)\n")
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.ok is False
    assert terminal.error.startswith(
        "harness_configuration: verdict serve refused the worker assignment"
    )
    assert terminal.status_code == 403
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action, verdict.scope) == (
        "harness_configuration",
        "BLOCK",
        "none",
    )
    assert verdict.cooldown_seconds == 0


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (403, "forbidden for this model"),
        (403, "account suspended: authentication failed for the provider key"),
        (401, "invalid api key"),
        (429, "authentication failed"),
        (503, "authentication failed"),
    ],
)
def test_other_denials_are_still_read_as_the_providers(
    tmp_path: Path, status: int, message: str
) -> None:
    line = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_x",
            "error": {"type": "provider.auth", "message": message, "status": status},
        }
    )
    binary = fake_worker(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.ok is False
    assert not terminal.error.startswith("harness_configuration:")
    assert terminal.status_code == status


def test_a_401_with_serves_refusal_message_is_the_same_block(tmp_path: Path) -> None:
    line = json.dumps(
        {
            "type": "error",
            "sessionID": "ses_x",
            "error": {"type": "provider.auth", "message": "Authentication failed.", "status": 401},
        }
    )
    binary = fake_worker(tmp_path, f"sys.stdout.write({line!r} + '\\n')\nsys.exit(1)\n")
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.error.startswith("harness_configuration: verdict serve refused")


# --- what serve recorded under the assignment, read back for the run receipt -------------

SCOPE = "server-auth"
WAIVER = {
    "requirement": "streaming",
    "reason": "verified_worker_assignment",
    "run_id": RUN_ID,
    "node_id": NODE_ID,
}


def seed_serve_root(db: Path, digest: str, waivers: list[dict[str, str]], n: int = 1) -> None:
    store = ReceiptStore(db, strict_scope=True)
    for index in range(n):
        store.put_receipt(
            "decision",
            SCOPE,
            {
                "routing_decision": {
                    "receipt": {
                        "worker_assignment": {"assignment_digest": digest, "route_id": ROUTE},
                        "requirement_waivers": waivers,
                    }
                }
            },
            receipt_id=f"evidence-{digest[:8]}-{index}",
        )


def worker_that_serve_sees(tmp_path: Path, db: Path, waivers: list[dict[str, str]], n: int) -> str:
    """A worker that, as serve would, leaves ``n`` roots under the assignment it was handed."""
    helper = tmp_path / "seed.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import hashlib, os, sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from tests.test_opencode_signed_assignment import seed_serve_root
            from pathlib import Path
            token = os.environ["VERDICT_WORKER_API_KEY"]
            digest = hashlib.sha256(token.split(".")[1].encode()).hexdigest()
            seed_serve_root(Path({str(db)!r}), digest, {waivers!r}, {n})
            """
        )
    )
    return fake_worker(
        tmp_path,
        f"import subprocess\nsys.exit(subprocess.call([{sys.executable!r}, {str(helper)!r}]))\n",
    )


def test_an_attempt_served_under_the_waiver_carries_it_on_its_terminal(tmp_path: Path) -> None:
    db = tmp_path / "serve-receipts.db"
    ReceiptStore(db, strict_scope=True)  # serve has already created its database
    binary = worker_that_serve_sees(tmp_path, db, [WAIVER], 3)
    terminal = go(executor(binary, receipts_db_provider=lambda: db), workdir(tmp_path))
    assert terminal.ok, terminal
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve == {
        "status": "read",
        "reason": None,
        "requests": 3,
        "requests_waived": 3,
        "requirement_waivers": [
            {"requirement": "streaming", "reason": "verified_worker_assignment"}
        ],
    }


def test_an_attempt_that_was_not_waived_carries_none(tmp_path: Path) -> None:
    db = tmp_path / "serve-receipts.db"
    ReceiptStore(db, strict_scope=True)
    binary = worker_that_serve_sees(tmp_path, db, [], 2)
    terminal = go(executor(binary, receipts_db_provider=lambda: db), workdir(tmp_path))
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert (serve["status"], serve["requests"], serve["requests_waived"]) == ("read", 2, 0)
    assert serve["requirement_waivers"] == []


def test_a_waiver_from_before_the_attempt_started_is_not_attributed_to_it(tmp_path: Path) -> None:
    """The same clock second and the same node would give two attempts one digest; only rows
    written after the worker started are looked at."""
    db = tmp_path / "serve-receipts.db"
    binary, seen = recording_worker(tmp_path)
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    ex = executor(binary, receipts_db_provider=lambda: db, clock=lambda: now)
    ReceiptStore(db, strict_scope=True)
    go(ex, workdir(tmp_path))  # first attempt: learn the digest it would have
    digest = wa.assignment_digest(seen.read_text())
    seed_serve_root(db, digest, [WAIVER], 2)  # serve wrote these for the first attempt
    terminal = go(ex, workdir(tmp_path))  # second attempt, same clock second, same ids
    assert terminal.worker_assignment["assignment_digest"] == digest  # type: ignore[index]
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert (serve["requests"], serve["requests_waived"]) == (0, 0)


@pytest.mark.parametrize(
    ("provider", "reason"),
    [
        (lambda: None, "not configured"),
        (lambda: ":memory:", "in memory"),
        (lambda: "/nonexistent-dir-1325/serve.db", "does not exist"),
    ],
    ids=["unconfigured", "memory", "missing"],
)
def test_unreadable_serve_receipts_are_unavailable_with_a_reason_and_the_attempt_still_ends_ok(
    tmp_path: Path, provider: Any, reason: str
) -> None:
    binary, _ = recording_worker(tmp_path)
    terminal = go(executor(binary, receipts_db_provider=provider), workdir(tmp_path))
    assert terminal.ok, "an unreadable receipt must not fail an attempt that worked"
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve["status"] == "unavailable" and reason in serve["reason"]
    assert serve["requests"] is None and serve["requests_waived"] is None
    assert serve["requirement_waivers"] is None


def test_a_reader_that_raises_is_unavailable_never_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(mark: Any, digest: str) -> Any:
        raise RuntimeError("row detail that must not be recorded")

    monkeypatch.setattr(executor_module, "read_assignment_evidence", boom)
    binary, _ = recording_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.ok
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve["status"] == "unavailable" and "RuntimeError" in serve["reason"]
    assert "row detail" not in json.dumps(terminal.worker_assignment)


def test_an_attempt_that_never_launched_a_worker_has_no_assignment_record(tmp_path: Path) -> None:
    binary, ran = marker_worker(tmp_path)
    ex = executor(binary)
    executor_module_check = executor_module._check_serve
    try:
        executor_module._check_serve = lambda *a, **k: "verdict serve is unreachable at x"
        terminal = go(ex, workdir(tmp_path))
    finally:
        executor_module._check_serve = executor_module_check
    assert terminal.error.startswith("harness_configuration:")
    assert not ran.exists() and terminal.worker_assignment is None


def test_a_receipt_database_resolver_that_raises_does_not_stop_the_attempt(tmp_path: Path) -> None:
    def boom() -> Path:
        raise RuntimeError(f"detail {SERVE_TOKEN}")

    binary, seen = recording_worker(tmp_path)
    terminal = go(executor(binary, receipts_db_provider=boom), workdir(tmp_path))
    assert terminal.ok, terminal  # the worker ran and its edit would have counted
    assert seen.read_text().startswith("vwa1.")
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve["status"] == "unavailable" and "RuntimeError" in serve["reason"]
    assert serve["requests"] is None and SERVE_TOKEN not in json.dumps(terminal.worker_assignment)


def test_a_mark_that_uses_up_the_budget_launches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mark can wait on a lock for a second; an attempt it has run out of time does not start."""
    import time

    def slow_mark(path: Any) -> Any:
        time.sleep(1.2)
        return executor_module.EvidenceMark(None, 0, "slow")

    monkeypatch.setattr(executor_module, "take_mark", slow_mark)
    binary, ran = marker_worker(tmp_path)
    cwd = workdir(tmp_path)
    terminal = go(executor(binary), cwd, timeout=1.0)
    assert terminal.ok is False and terminal.error == "timeout"
    assert not ran.exists(), "the worker was launched after the budget was spent"
    assert terminal.worker_assignment is None
    assert list(cwd.iterdir()) == []
