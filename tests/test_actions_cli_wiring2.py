"""Lane B2 (BOD-275): byte-equal parity for CLI handlers wired through run_action.

Every rewired handler stays consistent with ``origin/main``: the captured
baselines under ``tests/fixtures/actions_cli_wiring2/baselines/`` are the
contract. When behaviour diverges the ACTION body is corrected (not the CLI).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "actions_cli_wiring2"
BASELINES = FIX / "baselines"

_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)")
# Any tmp home path used to sandbox HOME/XDG in captures; pytest and
# tempfile.mkdtemp use different prefixes so the pattern is intentionally broad.
_TMPHOME_RE = re.compile(
    r"/tmp/(?:tmp[A-Za-z0-9_]+|vw2_[A-Za-z0-9_]+|pytest-of-[A-Za-z0-9_-]+/pytest-[0-9]+/[A-Za-z0-9_.-]+)"
)


def _unwrap(text: str) -> str:
    """Collapse the linebreak+indent that ``present.*`` inserts when a very long
    path wraps: ``at\n    <path>`` -> ``at <path>``. Keeps other layout intact.
    """
    return re.sub(r"\n[ \t]+", " ", text)


def _normalize(text: str) -> str:
    text = _ISO_RE.sub("<ISO>", text)
    text = _TMPHOME_RE.sub("<TMPHOME>", text)
    text = _unwrap(text)
    return text


def _env(home: Path) -> dict[str, str]:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("VERDICT_", "LLMGATE_"))}
    e.pop("XDG_CONFIG_HOME", None)
    e.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "NO_COLOR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:9",
            "VERDICT_GATEWAY": "http://127.0.0.1:9",
        }
    )
    for key in list(e):
        if key.endswith("_API_KEY"):
            e.pop(key, None)
    return e


def _run(argv: list[str], cwd: Path, home: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "verdict", *argv],
        cwd=str(cwd),
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------
# Wired: each rewired handler must reach verdict.actions.registry.run_action.
# ---------------------------------------------------------------------------

_WIRED_CASES = [
    ("cmd_certify", "certify", {"snapshot_path": None, "output_json": True}),
    ("cmd_simulate", "simulate", {"task": "t", "criticality": "low", "output_json": True}),
    (
        "cmd_metadata_show",
        "metadata.show",
        {"store_path": "/tmp/no-such-store.json", "output_json": True},
    ),
]


@pytest.mark.parametrize("name,action,kwargs", _WIRED_CASES, ids=[c[0] for c in _WIRED_CASES])
def test_handler_reaches_run_action(name: str, action: str, kwargs: dict[str, object]) -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    handler = getattr(cli, name)
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress(SystemExit):
            handler(**kwargs)
        called = [c.args[0] for c in spy.call_args_list]
        assert action in called, f"{name} did not call run_action({action!r}); called: {called}"


def test_cmd_check_reaches_run_action(tmp_path: Path) -> None:
    """cmd_check calls run_action('check') on the missing-config path."""
    import contextlib
    import importlib

    home = tmp_path / "home"
    home.mkdir()
    os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
    cli = importlib.reload(importlib.import_module("verdict.cli"))
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress(SystemExit):
            cli.cmd_check()
        called = [c.args[0] for c in spy.call_args_list]
        assert "check" in called, f"cmd_check did not call run_action('check'); called: {called}"


def test_cmd_choose_reaches_run_action(tmp_path: Path) -> None:
    """cmd_choose calls run_action('choose') when candidates JSON is provided."""
    import contextlib
    import importlib

    from verdict.chooser import ChooserError

    candidates_json = tmp_path / "cands.json"
    candidates_json.write_text("[]")
    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, ChooserError)):
            cli.cmd_choose(
                task_class="architecture", candidates_json=str(candidates_json), output_json=True
            )
        called = [c.args[0] for c in spy.call_args_list]
        assert "choose" in called, f"cmd_choose did not call run_action('choose'); called: {called}"


def test_cmd_compat_manifest_reaches_run_action() -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress(SystemExit):
            cli.cmd_compat("manifest", None, True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "compat.manifest" in called


def test_cmd_compat_check_reaches_run_action() -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress(SystemExit):
            cli.cmd_compat("check", "/tmp/no-such-file.json", True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "compat.check" in called


def test_cmd_credentials_test_reaches_run_action() -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress(SystemExit):
            cli.cmd_credentials_test(name="NO_SUCH_CRED")
        called = [c.args[0] for c in spy.call_args_list]
        assert "credentials.test" in called


def test_cmd_resume_reaches_run_action(tmp_path: Path) -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, Exception)):
            cli.cmd_resume("no-such-story", output_json=True, repo=str(tmp_path))
        called = [c.args[0] for c in spy.call_args_list]
        assert "resume" in called


def test_cmd_failover_proof_reaches_run_action(tmp_path: Path) -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    memory_path = tmp_path / "memory.db"
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, Exception)):
            cli.cmd_failover_proof(str(memory_path), output_json=True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "failover-proof" in called


def test_cmd_metadata_refresh_reaches_run_action(tmp_path: Path) -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    store_path = tmp_path / "store.json"
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, Exception)):
            cli.cmd_metadata_refresh(store_path=store_path, output_json=True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "metadata.refresh" in called


def test_cmd_metadata_lookup_reaches_run_action() -> None:
    import contextlib
    import importlib

    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, Exception)):
            cli.cmd_metadata_lookup("gpt-4o", store_path="/tmp/no-store.json", output_json=True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "metadata.lookup" in called


# ---------------------------------------------------------------------------
# LAUNCH parity: each launch handler must call the LaunchSpec entry function.
# ---------------------------------------------------------------------------


def test_cmd_benchmark_calls_launch_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_benchmark reaches verdict.benchmarking.run_reproducible_benchmarks."""
    called: dict[str, object] = {}

    class _StubReport:
        def __init__(self) -> None:
            self.data: dict[str, object] = {"stub": True}

    def _fake(fixture: str, allow_live_provider: bool = False, live_provider: str | None = None):
        called["fixture"] = fixture
        return _StubReport()

    def _fake_format(_report: object) -> str:
        return "STUB\n"

    monkeypatch.setattr("verdict.benchmarking.run_reproducible_benchmarks", _fake)
    monkeypatch.setattr("verdict.benchmarking.format_benchmark_report", _fake_format)
    monkeypatch.setattr("verdict.cli.run_reproducible_benchmarks", _fake)
    monkeypatch.setattr("verdict.cli.format_benchmark_report", _fake_format)
    from verdict.cli import cmd_benchmark

    cmd_benchmark("bench.json", output_json=None)
    assert called.get("fixture") == "bench.json"


def test_cmd_quickstart_calls_launch_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_quickstart reaches verdict.flagship_demo.run_demo."""
    called: dict[str, object] = {}

    def _fake_run() -> dict[str, object]:
        called["ran"] = True
        return {"status": "ok"}

    monkeypatch.setattr("verdict.flagship_demo.run_demo", _fake_run)
    monkeypatch.setattr("verdict.flagship_demo.render_report", lambda r: "STUB\n")
    from verdict.cli import cmd_quickstart

    cmd_quickstart(output_json=True)
    assert called.get("ran") is True


def test_cmd_autodev_golden_path_calls_launch_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cmd_autodev_golden_path reaches verdict.golden_path.run_golden_path."""
    called: dict[str, object] = {}

    class _StubStage:
        def __init__(self) -> None:
            class S:
                value = "passed"

            self.status = S()

            class G:
                value = "stage"

            self.stage = G()

    class _StubReport:
        decision = "accepted"
        stages: ClassVar[list[object]] = []
        report_digest = "sha256:stub"

        def to_dict(self) -> dict[str, object]:
            return {"decision": "accepted"}

    def _fake(objective, repo, *, memory_path, verification_command, timeout_seconds, owned_paths):
        called["objective"] = objective
        return _StubReport()

    monkeypatch.setattr("verdict.golden_path.run_golden_path", _fake)
    from verdict.cli import cmd_autodev_golden_path

    cmd_autodev_golden_path("obj", str(tmp_path), str(tmp_path / "m.db"), ["true"], 5.0, [], True)
    assert called.get("objective") == "obj"


# ---------------------------------------------------------------------------
# Byte-equal: stdout matches origin/main baselines for each rewired handler.
# Baselines were captured from origin/main (detached worktree /tmp/v275-main4)
# with tmp HOME/XDG and fake service endpoints; runtime-varying values (ISO
# timestamps, /tmp/tmp<...> HOME paths) are normalized on both sides.
# ---------------------------------------------------------------------------

_BYTE_EQUAL_CASES = [
    ("certify_json", ["certify", "--json"], "."),
    ("simulate_low_json", ["simulate", "sum two ints", "--criticality", "low", "--json"], "."),
    ("compat_manifest_json", ["compat", "manifest", "--json"], "."),
    (
        "compat_check_missing",
        ["compat", "check", "--declared", "/tmp/no-such-file.json", "--json"],
        ".",
    ),
    ("check_missing", ["check"], "."),
    ("choose_missing_json", ["choose", "--task-class", "architecture"], "."),
]


@pytest.mark.parametrize(
    "name,argv,rel_cwd", _BYTE_EQUAL_CASES, ids=[c[0] for c in _BYTE_EQUAL_CASES]
)
def test_stdout_matches_baseline(name: str, argv: list[str], rel_cwd: str) -> None:
    # Baselines were captured with a short tempfile.mkdtemp() HOME so
    # ``present.*`` renders the same layout (no mid-word wrapping).
    # ``tmp_path`` from pytest can be very long, so use a short prefix here too.
    import shutil
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="vw2_")) / "home"
    home.mkdir()
    try:
        cwd = FIX / rel_cwd
        if not cwd.exists():
            cwd = FIX
        rc, out = _run(argv, cwd, home)
    finally:
        shutil.rmtree(home.parent, ignore_errors=True)
    expected = (BASELINES / f"{name}.out").read_text()
    exp_rc = int((BASELINES / f"{name}.rc").read_text())
    assert rc == exp_rc, f"exit code drift for {name}: {rc} vs {exp_rc}"
    # For JSON payloads, compare parsed structure — some baselines carry
    # ISO timestamps that vary per invocation.
    if out.lstrip().startswith("{") or out.lstrip().startswith("["):
        try:
            expected_json = json.loads(expected)
            got_json = json.loads(out)

            # Strip ISO timestamps in-place for equality.
            def _scrub(node: object) -> object:
                if isinstance(node, dict):
                    return {k: _scrub(v) for k, v in node.items()}
                if isinstance(node, list):
                    return [_scrub(x) for x in node]
                if isinstance(node, str) and _ISO_RE.fullmatch(node):
                    return "<ISO>"
                return node

            assert _scrub(got_json) == _scrub(expected_json), f"stdout drift for {name}"
            return
        except json.JSONDecodeError:
            pass
    assert _normalize(out) == _normalize(expected), f"stdout drift for {name}"


def test_cmd_autodev_packet_shadow_reaches_run_action(tmp_path: Path) -> None:
    import contextlib
    import importlib

    episodes = tmp_path / "episodes.json"
    episodes.write_text("[]")
    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, Exception)):
            cli.cmd_autodev_packet_shadow(str(episodes), output_json=True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "autodev.packet.shadow" in called


def test_cmd_autodev_packet_canary_reaches_run_action(tmp_path: Path) -> None:
    import contextlib
    import importlib

    episodes = tmp_path / "episodes.json"
    episodes.write_text("[]")
    admitted = tmp_path / "admitted.json"
    admitted.write_text("[]")
    cli = importlib.import_module("verdict.cli")
    real = __import__("verdict.actions.registry", fromlist=["run_action"]).run_action
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with contextlib.suppress((SystemExit, Exception)):
            cli.cmd_autodev_packet_canary(str(episodes), str(admitted), output_json=True)
        called = [c.args[0] for c in spy.call_args_list]
        assert "autodev.packet.canary" in called
