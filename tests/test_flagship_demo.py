"""Tests for the credential-free public flagship demo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scripts.flagship_demo import build_demo_result
from verdict.flagship_demo import render_report, run_demo

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
