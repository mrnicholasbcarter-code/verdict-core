"""The OpenCode executor signs one assignment per attempt and gives it to the worker (#1325).

The assignment (route, run id, node id, issued-at, expiry) is the worker's only credential for
``verdict serve``.  The serve token stays in the orchestrator's process, for the readiness check.
These tests drive the executor with fake workers (scripts that record what they are given); the
executor-to-serve path through the real app is in ``test_opencode_assignment_serve_path.py``.
Nothing here reads a real home, key or database, or contacts anything but loopback.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import math
import os
import stat
import sys
import textwrap
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
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

# The real pre-dispatch functions, captured before the autouse fixture replaces them.
REAL_CHECK_SERVE = executor_module._check_serve
REAL_CHECK_ASSIGNMENT = executor_module._check_assignment


class Calls:
    """What the pre-dispatch checks were asked."""

    def __init__(self) -> None:
        self.serve: list[tuple[str, str]] = []
        self.assignment: list[tuple[str, str, float]] = []


@pytest.fixture(autouse=True)
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """The binary runs; serve accepts the serve token and the attempt's own assignment, and each
    call is recorded.  Nothing here contacts a serve: not even the default address, which on a
    host may be a real one (the tests of the real checks use ``real_checks``)."""
    seen = Calls()

    def check_serve(origin: str, token: str, timeout: float) -> str | None:
        seen.serve.append((origin, token))
        return None

    def check_assignment(origin: str, assignment: str, timeout: float) -> str | None:
        seen.assignment.append((origin, assignment, timeout))
        return None

    monkeypatch.setattr(executor_module, "_check_binary", lambda *a, **k: None)
    monkeypatch.setattr(executor_module, "_check_serve", check_serve)
    monkeypatch.setattr(executor_module, "_check_assignment", check_assignment)
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
    assert calls.serve == [] and calls.assignment == [], (
        "serve was contacted for an attempt that cannot be signed"
    )
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
    assert not ran.exists() and calls.serve == [] and calls.assignment == []
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
    assert calls.serve == [] and calls.assignment == [], "no request of any kind went to serve"
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
    assert not ran.exists() and calls.serve == [] and calls.assignment == []


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
    assert not ran.exists() and calls.serve == [] and calls.assignment == []
    assert list(cwd.iterdir()) == []
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
    assert not ran.exists() and calls.serve == [] and calls.assignment == []


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
    assert not ran.exists() and calls.serve == [] and calls.assignment == []


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
#
# A refusal and a provider's own 401 or 403 can say the same words, because serve forwards an
# upstream error unchanged.  Serve's receipts tell them apart: serve writes a root for every
# request it admits and none for one it refuses at the door.  But the receipt database is found
# from this host's configuration, not from the serve at the address the worker called, so an empty
# read of it proves nothing until the database has been seen to hold a request of this executor's
# own assignments (a row carrying a digest only this executor issued, that only the serve at the
# selected address received).  The tests below run the executor against a database serve has been
# seen writing and one it has not, one with a request recorded, and receipts that cannot be read;
# the words and the status are the same in all.

REFUSAL = (FIXTURES / "v2_3_2_error_403_serve_refusal.jsonl").read_text()


def error_line(message: str, status: int) -> str:
    return json.dumps(
        {
            "type": "error",
            "sessionID": "ses_x",
            "error": {"type": "provider.auth", "message": message, "status": status},
        }
    )


def empty_serve_database(tmp_path: Path) -> Path:
    """Serve's receipt database as it stands when serve has admitted nothing."""
    db = tmp_path / "serve-receipts.db"
    ReceiptStore(db, strict_scope=True)
    return db


def failing_worker(tmp_path: Path, stdout: str) -> str:
    return fake_worker(tmp_path, f"sys.stdout.write({stdout!r})\nsys.exit(1)\n")


def admitted_then_failing_worker(tmp_path: Path, db: Path, stdout: str) -> str:
    """A worker that, on its first run, is served (serve writes a root under its assignment) and
    succeeds, and on every later run fails with ``stdout`` without serve recording anything."""
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
            seed_serve_root(Path({str(db)!r}), digest, [], 1)
            """
        )
    )
    ran = tmp_path / "already-ran.txt"
    return fake_worker(
        tmp_path,
        f"import subprocess\n"
        f"if not os.path.exists({str(ran)!r}):\n"
        f"    open({str(ran)!r}, 'w').write('1')\n"
        f"    sys.exit(subprocess.call([{sys.executable!r}, {str(helper)!r}]))\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit(1)\n",
    )


def serve_seen_writing(tmp_path: Path, db: Path, stdout: str) -> OpenCodeExecutor:
    """An executor that has had one attempt served in ``db``, so it has seen serve write there.

    Its next attempt fails with ``stdout`` and serve records nothing under it.
    """
    ex = executor(
        admitted_then_failing_worker(tmp_path, db, stdout), receipts_db_provider=lambda: db
    )
    first = go(ex, workdir(tmp_path))
    assert first.ok, first
    assert first.worker_assignment["serve_receipts"]["requests"] == 1  # type: ignore[index]
    return ex


def test_serves_refusal_of_the_assignment_is_a_configuration_block_not_a_provider_denial(
    tmp_path: Path,
) -> None:
    """Captured from the real OpenCode 2.3.2 against a loopback stand-in for serve (see the
    fixtures README).  Read as plain 403 it would cool the provider down for an hour."""
    db = empty_serve_database(tmp_path)
    terminal = go(serve_seen_writing(tmp_path, db, REFUSAL), workdir(tmp_path))
    assert terminal.ok is False
    assert terminal.error.startswith(
        "harness_configuration: verdict serve refused the worker assignment"
    )
    assert terminal.status_code == 403
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert (serve["status"], serve["requests"]) == ("read", 0)
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action, verdict.scope) == (
        "harness_configuration",
        "BLOCK",
        "none",
    )
    assert verdict.cooldown_seconds == 0


def test_a_401_with_serves_refusal_message_is_the_same_block(tmp_path: Path) -> None:
    db = empty_serve_database(tmp_path)
    line = error_line("Authentication failed.", 401)
    terminal = go(serve_seen_writing(tmp_path, db, line + "\n"), workdir(tmp_path))
    assert terminal.error.startswith("harness_configuration: verdict serve refused")
    assert terminal.status_code == 401


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
    """With serve seen writing the database and nothing recorded under the assignment (so the
    receipts alone would say 'refused') a failure that does not have serve's status and words is
    still the provider's."""
    db = empty_serve_database(tmp_path)
    line = error_line(message, status)
    terminal = go(serve_seen_writing(tmp_path, db, line + "\n"), workdir(tmp_path))
    assert terminal.ok is False
    assert not terminal.error.startswith("harness_configuration:")
    assert terminal.status_code == status
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert (serve["status"], serve["requests"]) == ("read", 0)


def test_serves_words_are_the_providers_while_the_database_is_not_shown_to_be_serves(
    tmp_path: Path,
) -> None:
    """The database is found from this host's configuration: with a spare or remote serve it can
    be another serve's, or one nothing writes, and then an empty read is not "serve refused".
    Nothing has shown that this database is the selected serve's (no request of this executor's
    assignments is in it), so the words stay the provider's, with the cooldown that implies, and
    the run receipt says the receipts are unavailable rather than that serve recorded nothing."""
    db = empty_serve_database(tmp_path)
    terminal = go(
        executor(failing_worker(tmp_path, REFUSAL), receipts_db_provider=lambda: db),
        workdir(tmp_path),
    )
    assert terminal.ok is False
    assert not terminal.error.startswith("harness_configuration:")
    assert terminal.error == "authentication failed"
    assert terminal.status_code == 403
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve["status"] == "unavailable"
    assert "not shown to be" in serve["reason"]
    assert (serve["requests"], serve["requests_waived"], serve["requirement_waivers"]) == (
        None,
        None,
        None,
    )
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert verdict.category != "harness_configuration"
    assert verdict.cooldown_seconds > 0


def test_what_was_seen_in_one_database_does_not_vouch_for_another(tmp_path: Path) -> None:
    """Serve was seen writing the first database.  If the next attempt's database is a different
    file (a spare serve's, a re-pointed configuration), an empty read of it proves nothing."""
    seen_db = empty_serve_database(tmp_path)
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    other_db = empty_serve_database(other_dir)
    current = {"db": seen_db}
    ex = executor(
        admitted_then_failing_worker(tmp_path, seen_db, REFUSAL),
        receipts_db_provider=lambda: current["db"],
    )
    assert go(ex, workdir(tmp_path)).ok
    # still the same database: the refusal is serve's
    again = go(ex, workdir(tmp_path))
    assert again.error.startswith("harness_configuration: verdict serve refused")
    # a different database: nothing is shown about it
    current["db"] = other_db
    elsewhere = go(ex, workdir(tmp_path))
    assert not elsewhere.error.startswith("harness_configuration:")
    assert elsewhere.error == "authentication failed"
    assert elsewhere.worker_assignment["serve_receipts"]["status"] == "unavailable"  # type: ignore[index]
    # and back to the first: it is shown again
    current["db"] = seen_db
    back = go(ex, workdir(tmp_path))
    assert back.error.startswith("harness_configuration: verdict serve refused")


@pytest.mark.parametrize("change", ["replaced", "retargeted", "replaced-during-the-attempt"])
def test_what_was_seen_of_a_database_does_not_outlast_the_file(tmp_path: Path, change: str) -> None:
    """The proof is a request found in one file.  A path can be replaced, or a link retargeted, with
    another valid receipt database that serve never wrote; the path is unchanged but the file is
    not, so an empty read of it proves nothing (neither between attempts nor during one)."""
    first_dir, other_dir = tmp_path / "first", tmp_path / "other"
    first_dir.mkdir()
    other_dir.mkdir()
    real = empty_serve_database(first_dir)
    spare = empty_serve_database(other_dir)  # another valid, empty receipt database
    path = tmp_path / "serve-receipts-link.db"
    path.symlink_to(real)
    where = path if change == "retargeted" else real
    during = f"os.replace({str(spare)!r}, {str(real)!r})\n" if change.endswith("attempt") else ""

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
            seed_serve_root(Path({str(where)!r}), digest, [], 1)
            """
        )
    )
    ran = tmp_path / "already-ran.txt"
    binary = fake_worker(
        tmp_path,
        f"import subprocess\n"
        f"if not os.path.exists({str(ran)!r}):\n"
        f"    open({str(ran)!r}, 'w').write('1')\n"
        f"    sys.exit(subprocess.call([{sys.executable!r}, {str(helper)!r}]))\n"
        f"{during}"
        f"sys.stdout.write({REFUSAL!r})\n"
        f"sys.exit(1)\n",
    )
    ex = executor(binary, receipts_db_provider=lambda: where)
    assert go(ex, workdir(tmp_path)).ok

    if change == "replaced":
        os.replace(spare, real)
    elif change == "retargeted":
        path.unlink()
        path.symlink_to(spare)
    refused = go(ex, workdir(tmp_path))

    assert refused.ok is False
    assert not refused.error.startswith("harness_configuration:"), refused.error
    assert refused.error == "authentication failed"
    serve = refused.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve["status"] == "unavailable"
    assert serve["requests"] is None
    verdict = FailureIntelligence().classify(refused, now=datetime.now(timezone.utc))
    assert verdict.category != "harness_configuration"
    assert verdict.cooldown_seconds > 0


def test_an_attempt_that_serve_never_served_does_not_show_the_database_to_be_serves(
    tmp_path: Path,
) -> None:
    """Only a request found under an assignment shows the database: an attempt that succeeded
    with serve having recorded nothing (the worker never called it) shows nothing."""
    db = empty_serve_database(tmp_path)
    ex = executor(
        fake_worker(
            tmp_path,
            f"if not os.path.exists({str(tmp_path / 'ran')!r}):\n"
            f"    open({str(tmp_path / 'ran')!r}, 'w').write('1')\n"
            "    sys.exit(0)\n"
            f"sys.stdout.write({REFUSAL!r})\n"
            "sys.exit(1)\n",
        ),
        receipts_db_provider=lambda: db,
    )
    cwd = workdir(tmp_path)
    first = go(ex, cwd)
    assert first.ok
    assert first.worker_assignment["serve_receipts"]["status"] == "unavailable"  # type: ignore[index]
    second = go(ex, cwd)
    assert not second.error.startswith("harness_configuration:")
    assert second.worker_assignment["serve_receipts"]["status"] == "unavailable"  # type: ignore[index]


@pytest.mark.parametrize("status", [401, 403])
def test_serves_words_after_a_request_serve_admitted_are_the_providers(
    tmp_path: Path, status: int
) -> None:
    """Serve forwards a provider's own 401 or 403 unchanged, so the same words can come from the
    upstream.  Serve's receipts hold a request under this assignment (it was admitted), so the
    error is the provider's: recovery must cool the provider down, not block the harness."""
    db = empty_serve_database(tmp_path)
    binary = worker_that_serve_sees(
        tmp_path, db, [], 1, stdout=error_line("authentication failed", status) + "\n", code=1
    )
    terminal = go(executor(binary, receipts_db_provider=lambda: db), workdir(tmp_path))
    assert terminal.ok is False
    assert terminal.error == "authentication failed"
    assert terminal.status_code == status
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert (serve["status"], serve["requests"]) == ("read", 1)
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert verdict.category != "harness_configuration"
    assert verdict.cooldown_seconds > 0


def unreadable_database(tmp_path: Path) -> Path:
    db = tmp_path / "not-a-database.db"
    db.write_bytes(b"this is not an sqlite file" * 40)
    return db


@pytest.mark.parametrize(
    "receipts",
    [
        pytest.param(lambda tmp: None, id="not-configured"),
        pytest.param(lambda tmp: ":memory:", id="in-memory"),
        pytest.param(lambda tmp: tmp / "never-created.db", id="missing"),
        pytest.param(unreadable_database, id="unreadable"),
    ],
)
def test_serves_words_are_the_providers_when_serves_receipts_cannot_be_read(
    tmp_path: Path, receipts: Any
) -> None:
    """If the origin of the error cannot be established it stays the provider's: it is never
    blocked on the words alone."""
    location = receipts(tmp_path)
    terminal = go(
        executor(failing_worker(tmp_path, REFUSAL), receipts_db_provider=lambda: location),
        workdir(tmp_path),
    )
    assert terminal.ok is False
    assert not terminal.error.startswith("harness_configuration:")
    assert terminal.error == "authentication failed"
    assert terminal.status_code == 403
    serve = terminal.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert serve["status"] == "unavailable"
    assert serve["requests"] is None
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert verdict.category != "harness_configuration"


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


def worker_that_serve_sees(
    tmp_path: Path,
    db: Path,
    waivers: list[dict[str, str]],
    n: int,
    *,
    stdout: str = "",
    code: int = 0,
) -> str:
    """A worker that, as serve would, leaves ``n`` roots under the assignment it was handed, then
    prints ``stdout`` and exits ``code``."""
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
        f"import subprocess\n"
        f"seeded = subprocess.call([{sys.executable!r}, {str(helper)!r}])\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit(seeded or {code})\n",
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


def test_a_request_of_an_earlier_attempt_is_not_attributed_to_a_retry_in_the_same_second(
    tmp_path: Path,
) -> None:
    """Two attempts of one node on one route, signed in one clock second, would hold one credential
    and one digest.  The second is dated a second later, so a request of the first that serve
    commits after the second's mark (here: while the second worker runs) is not the second's."""
    db = tmp_path / "serve-receipts.db"
    credential, flag = tmp_path / "credential.txt", tmp_path / "late-digest.txt"
    helper = tmp_path / "late.py"
    helper.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
            from pathlib import Path
            from tests.test_opencode_signed_assignment import seed_serve_root
            seed_serve_root(Path({str(db)!r}), sys.argv[1], [{WAIVER!r}], 2)
            """
        )
    )
    binary = fake_worker(
        tmp_path,
        f"import subprocess, hashlib\n"
        f"token = os.environ['VERDICT_WORKER_API_KEY']\n"
        f"open({str(credential)!r}, 'w').write(token)\n"
        f"if os.path.exists({str(flag)!r}):\n"
        f"    subprocess.call([{sys.executable!r}, {str(helper)!r}, open({str(flag)!r}).read()])\n"
        f"else:\n"
        f"    own = hashlib.sha256(token.split('.')[1].encode()).hexdigest()\n"
        f"    subprocess.call([{sys.executable!r}, {str(helper)!r}, own])\n"
        f"sys.exit(0)\n",
    )
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    ex = executor(binary, receipts_db_provider=lambda: db, clock=lambda: now)
    ReceiptStore(db, strict_scope=True)  # serve has created its database

    first = go(ex, workdir(tmp_path))
    first_digest = wa.assignment_digest(credential.read_text())
    assert first.worker_assignment["assignment_digest"] == first_digest  # type: ignore[index]

    flag.write_text(first_digest)  # from now on the worker also makes serve commit the first's rows
    second = go(ex, workdir(tmp_path))
    assert second.ok
    assert second.worker_assignment["assignment_digest"] != first_digest  # type: ignore[index]
    serve = second.worker_assignment["serve_receipts"]  # type: ignore[index]
    assert (serve["requests"], serve["requests_waived"]) == (0, 0)


def test_each_live_assignment_of_one_executor_has_its_own_identity_even_in_one_second(
    tmp_path: Path,
) -> None:
    binary, seen = recording_worker(tmp_path)
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    ex = executor(binary, clock=lambda: now)
    digests, issued = [], []
    for _ in range(3):
        go(ex, workdir(tmp_path), timeout=10.0)
        token = seen.read_text()
        digests.append(wa.assignment_digest(token))
        fields = sign.decode_payload(token)
        issued.append((fields["iat"], fields["exp"] - fields["iat"]))
    assert len(set(digests)) == 3
    base = int(now.timestamp())
    assert issued == [(base, 70), (base + 1, 70), (base + 2, 70)]
    # still accepted by serve's verifier at the real clock of the signer
    wa.verify_assignment(seen.read_text(), KEY, now=now)


def test_concurrent_attempts_with_the_same_ids_get_distinct_identities(tmp_path: Path) -> None:
    binary, _ = recording_worker(tmp_path)
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    ex = executor(binary, clock=lambda: now)

    async def both() -> list[Any]:
        cwd_a, cwd_b = tmp_path / "a", tmp_path / "b"
        cwd_a.mkdir()
        cwd_b.mkdir()
        return list(
            await asyncio.gather(
                *(
                    ex.run(
                        "task",
                        route_id=ROUTE,
                        cwd=cwd,
                        timeout_seconds=10.0,
                        run_id=RUN_ID,
                        node_id=NODE_ID,
                    )
                    for cwd in (cwd_a, cwd_b)
                )
            )
        )

    first, second = asyncio.run(both())
    assert first.ok and second.ok
    assert (
        first.worker_assignment["assignment_digest"]  # type: ignore[index]
        != second.worker_assignment["assignment_digest"]  # type: ignore[index]
    )


def test_an_expired_assignment_is_forgotten_so_the_clock_is_not_pushed_ahead_for_ever(
    tmp_path: Path,
) -> None:
    binary, seen = recording_worker(tmp_path)
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    clock = {"now": now}
    ex = executor(binary, clock=lambda: clock["now"])
    go(ex, workdir(tmp_path))
    first = sign.decode_payload(seen.read_text())
    clock["now"] = now + timedelta(seconds=71)  # the first assignment (70 s) has expired
    go(ex, workdir(tmp_path))
    assert sign.decode_payload(seen.read_text())["iat"] == int(clock["now"].timestamp())
    assert first["iat"] == int(now.timestamp())


def test_more_live_assignments_than_the_identity_window_allows_is_a_block_not_a_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_module, "MAX_IDENTITY_BUMP_SECONDS", 2)
    binary, _ran = marker_worker(tmp_path)
    now = datetime(2026, 11, 25, 12, 0, 0, tzinfo=timezone.utc)
    ex = executor(binary, clock=lambda: now)
    results = [go(ex, workdir(tmp_path)) for _ in range(4)]
    assert [r.ok for r in results] == [True, True, True, False]
    assert results[3].error.startswith("harness_configuration:")
    assert "distinct identity" in results[3].error


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
    def boom(self: Any, mark: Any, digest: str, *, origin: str) -> Any:
        raise RuntimeError("row detail that must not be recorded")

    monkeypatch.setattr(executor_module.SeenDatabases, "read", boom)
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


# --- the pre-launch assignment probe (#1340) ---------------------------------------------
#
# Before a worker is launched, the executor sends the attempt's own assignment to serve and reads
# the answer: 400 (serve's model check, which comes after it verified the assignment and before any
# routing) means "accepted"; serve's exact 403 refusal or its 503 "worker assignments are not
# configured" ends the attempt as a configuration block; anything else is "unproven" and the
# attempt goes on exactly as before.  These tests run the real checks against a scripted loopback
# server that records every request; the same probe against the real serve app is in
# ``test_opencode_assignment_serve_path.py``.

SERVE_REFUSAL = json.dumps(
    {"error": {"message": "authentication failed", "type": "authentication_error"}}
).encode()
SERVE_NOT_CONFIGURED = json.dumps(
    {"error": {"message": "worker assignments are not configured", "type": "api_error"}}
).encode()
SERVE_MODEL_CHECK = json.dumps(
    {
        "error": {
            "message": "the request's model must equal the route in the worker assignment",
            "type": "invalid_request_error",
        }
    }
).encode()


@dataclass
class Reply:
    """What the scripted serve answers to one kind of request."""

    status: int = 200
    body: bytes = b"{}"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0  # seconds before it answers
    hang_up: bool = False  # close the connection without any answer
    short_body: bool = False  # promise more bytes than are sent, then close


@dataclass
class SeenRequest:
    method: str
    path: str
    headers: dict[str, str]  # names lower-cased
    body: bytes


class ScriptedServe:
    """A loopback HTTP server that answers from a script and records every request."""

    def __init__(self, post: Reply, get: Reply | None = None) -> None:
        self.seen: list[SeenRequest] = []
        seen = self.seen
        replies = {"GET": get or Reply(200, b'{"status": "ok"}'), "POST": post}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _answer(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {name.lower(): value for name, value in self.headers.items()}
                seen.append(SeenRequest(self.command, self.path, headers, body))
                reply = replies[self.command]
                if reply.delay:
                    time.sleep(reply.delay)
                if reply.hang_up:
                    self.close_connection = True
                    return
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Content-Length", str(len(reply.body) + (100 if reply.short_body else 0))
                )
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(reply.body)
                    self.wfile.flush()
                except OSError:
                    pass
                if reply.short_body:
                    self.close_connection = True

            def do_GET(self) -> None:
                self._answer()

            def do_POST(self) -> None:
                self._answer()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def scripted() -> Iterator[Callable[..., ScriptedServe]]:
    servers: list[ScriptedServe] = []

    def make(post: Reply, get: Reply | None = None) -> ScriptedServe:
        server = ScriptedServe(post, get)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


@pytest.fixture
def real_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve's readiness check and the assignment probe run for real."""
    monkeypatch.setattr(executor_module, "_check_serve", REAL_CHECK_SERVE)
    monkeypatch.setattr(executor_module, "_check_assignment", REAL_CHECK_ASSIGNMENT)


def an_assignment() -> str:
    return wa.mint_assignment(
        KEY,
        route_id=ROUTE,
        run_id=RUN_ID,
        node_id=NODE_ID,
        lifetime_seconds=120,
        now=datetime.now(timezone.utc),
    )


def test_the_probe_model_is_no_route_id_a_verified_assignment_can_carry() -> None:
    """The probe's model must differ from the route of every assignment serve can verify: serve
    refuses a model that differs, before routing, which is what keeps the probe from being a
    spend.  A verified assignment's route id is a string of 1 to 128 characters without control
    characters, so the sentinel is one that the signer will not sign and the verifier rejects."""
    model = executor_module._ASSIGNMENT_PROBE_MODEL
    assert isinstance(model, str)
    with pytest.raises(ValueError):
        wa.mint_assignment(
            KEY,
            route_id=model,
            run_id=RUN_ID,
            node_id=NODE_ID,
            lifetime_seconds=60,
            now=datetime.now(timezone.utc),
        )
    issued = int(time.time())
    hand_signed = sign.assignment(
        KEY, route_id=model, run_id=RUN_ID, node_id=NODE_ID, exp=issued + 60, iat=issued
    )
    with pytest.raises(wa.AssignmentError) as refused:
        wa.verify_assignment(hand_signed, KEY, now=datetime.now(timezone.utc))
    assert refused.value.reason == wa.REASON_MALFORMED
    from tests.test_assigned_lane import ROUTES

    assert model not in {ROUTE, *ROUTES}
    body = json.loads(executor_module._assignment_probe_body())
    assert body == {"model": model}, "the probe sends the model and nothing else (no messages)"


def test_the_probe_carries_the_assignment_only_and_the_readiness_check_the_serve_token_only(
    tmp_path: Path, scripted: Callable[..., ScriptedServe], real_checks: None
) -> None:
    serve = scripted(Reply(400, SERVE_MODEL_CHECK))
    binary, credential_file = recording_worker(tmp_path)
    terminal = go(executor(binary, serve_base_url=serve.origin), workdir(tmp_path))
    assert terminal.ok, terminal  # accepted: the attempt went on and the worker ran
    credential = credential_file.read_text()
    assert credential.startswith("vwa1.")
    readiness, probe = serve.seen  # in that order, and nothing else
    assert (readiness.method, readiness.path) == ("GET", "/v1/guidance/status")
    assert readiness.headers["authorization"] == f"Bearer {SERVE_TOKEN}"
    assert credential not in json.dumps(readiness.headers)
    assert (probe.method, probe.path) == ("POST", "/v1/chat/completions")
    assert probe.headers["authorization"] == f"Bearer {credential}"
    assert SERVE_TOKEN not in json.dumps(probe.headers) + probe.body.decode()
    assert probe.headers["content-type"] == "application/json"
    assert json.loads(probe.body) == {"model": executor_module._ASSIGNMENT_PROBE_MODEL}
    # it is this attempt's own assignment: serve's verifier accepts it and reads the attempt's ids
    verified = wa.verify_assignment(credential, KEY, now=datetime.now(timezone.utc))
    assert (verified.route_id, verified.run_id, verified.node_id) == (ROUTE, RUN_ID, NODE_ID)


def blocked_terminal(serve: ScriptedServe, tmp_path: Path) -> tuple[Any, Path, Path]:
    binary, ran = marker_worker(tmp_path)
    cwd = workdir(tmp_path)
    terminal = go(executor(binary, serve_base_url=serve.origin), cwd)
    return terminal, ran, cwd


def assert_a_block_before_launch(terminal: Any, ran: Path, cwd: Path, serve: ScriptedServe) -> str:
    """Block, nothing launched or written, recovery blocks without a cooldown; the reason."""
    assert terminal.ok is False
    prefix = f"harness_configuration: verdict serve at {serve.origin} "
    assert terminal.error.startswith(prefix), terminal.error
    reason = terminal.error.removeprefix("harness_configuration: ")
    assert len(reason) <= 400, "a longer reason loses its beginning (the tail is what is kept)"
    assert not ran.exists(), "the worker was launched"
    assert list(cwd.iterdir()) == []
    assert terminal.worker_assignment is None and terminal.status_code is None
    assert SERVE_TOKEN not in terminal.error and "vwa1." not in terminal.error
    assert [request.method for request in serve.seen] == ["GET", "POST"]
    verdict = FailureIntelligence().classify(terminal, now=datetime.now(timezone.utc))
    assert (verdict.category, verdict.action, verdict.cooldown_seconds, verdict.scope) == (
        "harness_configuration",
        "BLOCK",
        0,
        "none",
    )
    return reason


def test_serves_exact_refusal_ends_the_first_attempt_before_launch_naming_the_causes(
    tmp_path: Path, scripted: Callable[..., ScriptedServe], real_checks: None
) -> None:
    serve = scripted(Reply(403, SERVE_REFUSAL))
    terminal, ran, cwd = blocked_terminal(serve, tmp_path)
    reason = assert_a_block_before_launch(terminal, ran, cwd, serve)
    assert "HTTP 403" in reason
    assert "worker-assignment.key" in reason and "HOME" in reason  # another key or home
    assert "60 s" in reason  # clocks
    assert "older than the signed-assignment change" in reason and "anonymous" in reason
    assert "verdict setup --apply --allow host.serve" in reason  # the fix


def test_serves_503_not_configured_ends_the_attempt_the_same_way_naming_setup(
    tmp_path: Path, scripted: Callable[..., ScriptedServe], real_checks: None
) -> None:
    serve = scripted(Reply(503, SERVE_NOT_CONFIGURED))
    terminal, ran, cwd = blocked_terminal(serve, tmp_path)
    reason = assert_a_block_before_launch(terminal, ran, cwd, serve)
    assert "HTTP 503" in reason and "no worker assignment signing key" in reason
    assert "verdict setup --apply --allow host.serve" in reason


@pytest.mark.parametrize("status", [403, 503])
def test_the_block_message_keeps_its_beginning_for_a_long_serve_address(status: int) -> None:
    """The recorded reason keeps its last 400 characters, so a longer one would lose the start
    that says which serve refused: a 60-character address must still fit."""
    origin = "http://" + "h" * 53
    assert len(origin) == 60
    reason = executor_module._probe_refusal_reason(origin, status)
    assert len(reason) <= 400, len(reason)
    assert reason.startswith(f"verdict serve at {origin} ")
    assert executor_module._sanitize(reason) == reason


UNPROVEN = [
    pytest.param(Reply(200, b'{"id": "x"}'), id="2xx"),
    pytest.param(Reply(204, b""), id="204-empty"),
    pytest.param(Reply(404, b'{"detail": "Not Found"}'), id="404"),
    pytest.param(Reply(500, b'{"error": "boom"}'), id="500"),
    pytest.param(Reply(502, b"<html>bad gateway</html>"), id="502-html"),
    pytest.param(Reply(503, b'{"detail": "Proxy not initialized"}'), id="503-not-initialized"),
    pytest.param(Reply(503, b""), id="503-empty"),
    pytest.param(
        Reply(403, b'{"error": {"message": "forbidden", "type": "authentication_error"}}'),
        id="403-another-body",
    ),
    pytest.param(Reply(403, b"<html>blocked by a proxy</html>"), id="403-html"),
    pytest.param(Reply(403, b""), id="403-empty"),
    pytest.param(
        Reply(
            403,
            b'{"error": {"message": "authentication failed", "type": "authentication_error"},'
            b' "extra": 1}',
        ),
        id="403-serves-words-and-more",
    ),
    pytest.param(Reply(403, SERVE_REFUSAL + b" " * 10_000), id="403-body-over-the-read-limit"),
    pytest.param(Reply(401, SERVE_REFUSAL), id="401-with-serves-403-body"),
    pytest.param(Reply(503, SERVE_REFUSAL), id="503-with-the-refusal-body"),
    pytest.param(Reply(403, SERVE_NOT_CONFIGURED), id="403-with-the-503-body"),
    pytest.param(Reply(400, b'{"error": "bad"}'), id="400-another-body"),
    pytest.param(Reply(403, b'{"error"', short_body=True), id="403-cut-short"),
    pytest.param(Reply(hang_up=True), id="connection-closed-without-an-answer"),
]


@pytest.mark.parametrize("reply", UNPROVEN)
def test_an_unclear_probe_result_is_unproven_and_the_attempt_goes_on_as_before(
    tmp_path: Path, scripted: Callable[..., ScriptedServe], real_checks: None, reply: Reply
) -> None:
    serve = scripted(reply)
    probe = executor_module._probe_assignment(serve.origin, an_assignment(), 5.0)
    assert probe.outcome == "unproven" and probe.reason is None
    binary, seen = recording_worker(tmp_path)
    terminal = go(executor(binary, serve_base_url=serve.origin), workdir(tmp_path))
    assert terminal.ok, terminal  # never a block, never a failure of the attempt
    assert seen.read_text().startswith("vwa1."), "the worker was launched with its assignment"
    assert not (terminal.error or "").startswith("harness_configuration:")


def test_a_probe_that_gets_no_answer_in_time_is_unproven_and_the_attempt_goes_on(
    tmp_path: Path,
    scripted: Callable[..., ScriptedServe],
    real_checks: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve = scripted(Reply(403, SERVE_REFUSAL, delay=2.0))
    started = time.monotonic()
    probe = executor_module._probe_assignment(serve.origin, an_assignment(), 0.3)
    assert probe.outcome == "unproven" and probe.reason is None
    assert time.monotonic() - started < 1.5, "the probe waited for the slow answer"
    monkeypatch.setattr(executor_module, "_SERVE_CHECK_TIMEOUT_SECONDS", 0.3)
    binary, seen = recording_worker(tmp_path)
    terminal = go(executor(binary, serve_base_url=serve.origin), workdir(tmp_path))
    assert terminal.ok, terminal
    assert seen.read_text().startswith("vwa1.")


def test_a_probe_to_nothing_listening_is_unproven() -> None:
    server = ScriptedServe(Reply())
    origin = server.origin
    server.close()  # the port now refuses connections
    probe = executor_module._probe_assignment(origin, an_assignment(), 2.0)
    assert (probe.outcome, probe.status, probe.reason) == ("unproven", None, None)


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_never_followed_so_the_assignment_goes_nowhere_else(
    tmp_path: Path, scripted: Callable[..., ScriptedServe], real_checks: None, code: int
) -> None:
    """urllib itself follows a 301, 302 or 303 answer to a POST (as a GET, carrying the
    Authorization header along), so these are what shows that the probe does not."""
    elsewhere = scripted(Reply(403, SERVE_REFUSAL))
    serve = scripted(Reply(code, b"", headers={"Location": f"{elsewhere.origin}/captured"}))
    probe = executor_module._probe_assignment(serve.origin, an_assignment(), 5.0)
    assert probe.outcome == "unproven"
    assert elsewhere.seen == [], "the assignment was sent to where the redirect pointed"
    binary, _credential = recording_worker(tmp_path)
    terminal = go(executor(binary, serve_base_url=serve.origin), workdir(tmp_path))
    assert terminal.ok, terminal
    assert elsewhere.seen == []


def test_the_probe_is_bounded_by_what_is_left_of_the_attempt(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    given: list[float] = []

    def probe(origin: str, assignment: str, timeout: float) -> str | None:
        given.append(timeout)
        return None

    monkeypatch.setattr(executor_module, "_check_assignment", probe)
    binary, _seen = recording_worker(tmp_path)
    assert go(executor(binary), workdir(tmp_path), timeout=2.0).ok
    assert go(executor(binary), workdir(tmp_path), timeout=100.0).ok
    short, long = given
    assert 0 < short <= 2.0, "the probe may take only what the attempt has left"
    assert 0 < long <= executor_module._SERVE_CHECK_TIMEOUT_SECONDS


def test_a_failed_readiness_check_ends_the_attempt_with_no_probe(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        executor_module, "_check_serve", lambda *a, **k: "verdict serve is unreachable at x"
    )
    binary, ran = marker_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path))
    assert terminal.error == "harness_configuration: verdict serve is unreachable at x"
    assert calls.assignment == [], "the assignment was sent to a serve that failed its readiness"
    assert not ran.exists()


def test_a_readiness_check_that_uses_up_the_budget_leaves_no_time_for_the_probe(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(origin: str, token: str, timeout: float) -> None:
        time.sleep(1.2)

    monkeypatch.setattr(executor_module, "_check_serve", slow)
    binary, ran = marker_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path), timeout=1.0)
    assert terminal.ok is False and terminal.error == "timeout"
    assert calls.assignment == [] and not ran.exists()


def test_a_refusal_that_arrives_after_the_attempts_deadline_is_a_timeout_not_a_block(
    tmp_path: Path, calls: Calls, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like the readiness check: a check that ran into the attempt's own deadline is the
    attempt's timeout, never a verdict about serve."""

    def late_refusal(origin: str, assignment: str, timeout: float) -> str:
        time.sleep(1.2)
        return f"verdict serve at {origin} refused this attempt's worker assignment"

    monkeypatch.setattr(executor_module, "_check_assignment", late_refusal)
    binary, ran = marker_worker(tmp_path)
    terminal = go(executor(binary), workdir(tmp_path), timeout=1.0)
    assert terminal.ok is False and terminal.error == "timeout"
    assert not ran.exists()


def test_the_probe_cannot_raise_into_the_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripted: Callable[..., ScriptedServe]
) -> None:
    """Whatever goes wrong while sending or reading the probe is 'unproven', never a failure."""
    serve = scripted(Reply(403, SERVE_REFUSAL))

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("unforeseen")

    import verdict.http_safety as http_safety

    monkeypatch.setattr(http_safety, "open_no_redirect", explode)
    probe = executor_module._probe_assignment(serve.origin, an_assignment(), 5.0)
    assert (probe.outcome, probe.status, probe.reason) == ("unproven", None, None)
