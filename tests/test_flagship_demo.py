"""Tests for the credential-free public flagship demo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.flagship_demo import build_demo_result
from verdict.flagship_demo import build_trusted_change_report_demo, render_report, run_demo

ROOT = Path(__file__).parents[1]


def test_fixture_has_one_eligible_candidate_and_explains_exclusions() -> None:
    result = build_demo_result()

    assert result["eligible"] == ["demo/frontier-tools"]
    exclusions = {row["model"]: row["reason"] for row in result["decision"]["exclusions"]}
    assert exclusions == {
        "demo/no-tools": "missing capability: tools",
        "demo/quota-empty": "quota exhausted",
        "demo/unverified": "health unknown",
    }
    assert result["decision"]["selected_route"]["runtime_id"] == "demo/frontier-tools"
    assert "api_key" not in json.dumps(result).lower()


def test_cli_output_is_reproducible_without_network_or_credentials() -> None:
    command = [sys.executable, "scripts/flagship_demo.py"]
    first = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    second = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)

    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["decision"]["planner_mode"] == "deterministic_fixture"


def test_cli_output_ignores_provider_environment_variables() -> None:
    command = [sys.executable, "scripts/flagship_demo.py"]
    baseline = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
    env = {
        **os.environ,
        "OPENAI_API_KEY": "sk-demo-openai",
        "ANTHROPIC_API_KEY": "sk-demo-anthropic",
        "LLMGATE_UPSTREAM_API_KEY": "sk-demo-upstream",
    }
    with_env = subprocess.run(
        command, cwd=ROOT, env=env, check=True, capture_output=True, text=True
    )

    assert baseline.stdout == with_env.stdout


def test_packaged_demo_matches_source_wrapper() -> None:
    assert run_demo() == build_demo_result()
    assert "Status: PASS" in render_report(run_demo())


def test_trusted_change_report_demo_accepts_and_is_deterministic() -> None:
    """The TCR demo projects a clean change and accepts it, fully deterministically."""
    first = build_trusted_change_report_demo()
    second = build_trusted_change_report_demo()

    # Source-bound, fail-closed verdict.
    assert first["verdict"]["decision"] == "accepted"
    assert first["verdict"]["reason"] == "ACCEPTED_ALL_GATES_GREEN"
    assert first["report"]["source_state"]["commit_sha"]
    # Deterministic across runs (no time.time() leaks).
    assert first["report"] == second["report"]
    assert first["redacted"] == second["redacted"]


def test_trusted_change_report_demo_redacted_is_leak_free() -> None:
    """The redacted export carries no secrets and no producer-internal fields."""
    result = build_trusted_change_report_demo()
    redacted = result["redacted"]
    # The stamped verdict is carried into the portable report.
    assert redacted["acceptance"]["decision"] == "accepted"
    # Producer-internal fields are dropped.
    for vr in redacted["verification_results"]:
        assert "raw_output" not in vr
        assert "command" not in vr
        assert "runtime" not in vr
    # No provider credentials survive.
    text = json.dumps(redacted)
    assert "OPENAI_API_KEY" not in text
    assert "sk-demo" not in text
    assert "api_key" not in text.lower() or "no_api_key" in text.lower()


def test_installed_cli_quickstart_is_json_and_read_only(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-m",
        "verdict",
        "quickstart",
        "--json",
        "--non-interactive",
        "--dry-run",
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    payload = json.loads(result.stdout)
    assert payload["decision"]["selected_route"]["runtime_id"] == "demo/frontier-tools"
    assert list(tmp_path.iterdir()) == []


def test_quickstart_failure_returns_nonzero(monkeypatch: object, capsys: object) -> None:
    import verdict.flagship_demo
    from verdict import cli

    monkeypatch.setattr(
        verdict.flagship_demo, "run_demo", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )  # type: ignore[attr-defined]
    try:
        cli.cmd_quickstart(output_json=True)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("quickstart failure did not exit nonzero")
    assert '"status": "fail"' in capsys.readouterr().out  # type: ignore[attr-defined]
