"""Tests for the credential-free public flagship demo."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.flagship_demo import build_demo_result
from verdict.contracts import TrustedChangeReport
from verdict.flagship_demo import (
    build_denied_trusted_change_report_demo,
    build_trusted_change_report_demo,
    render_report,
    run_accepted_and_denied_demo,
    run_demo,
)
from verdict.trusted_change_report import canonical_report_payload, compute_report_digest

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
    assert payload["accepted"]["verdict"]["decision"] == "accepted"
    assert payload["denied"]["verdict"]["decision"] == "denied"


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
    assert "git@" not in text
    assert "https://" not in text
    assert "http://" not in text
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


# ---------------------------------------------------------------------------
# Feature 005 — Accepted + Denied Trusted Change Report demo (gate 1, #266)
# ---------------------------------------------------------------------------


def test_denied_trusted_change_report_demo_denies_failed_gate() -> None:
    """The denied leg projects a FAILED verification gate and denies, deterministically."""
    first = build_denied_trusted_change_report_demo()
    second = build_denied_trusted_change_report_demo()

    # Source-bound, fail-closed verdict: a genuinely-failed gate → DENIED_FAILED_CHECK.
    assert first["verdict"]["decision"] == "denied"
    assert first["verdict"]["reason"] == "DENIED_FAILED_CHECK"
    assert first["report"]["source_state"]["commit_sha"]
    # Deterministic across runs (no time.time() leaks).
    assert first["report"] == second["report"]
    assert first["redacted"] == second["redacted"]


def test_denied_trusted_change_report_demo_route_stays_eligible() -> None:
    """The denial is solely from the failed gate; the projected route is the healthy one.

    The denied leg reuses build_demo_result()'s routing decision, so the selected route
    matches the accepted leg — the verdict diverges only because of the failed check,
    not because of an ineligible route (honest 'denied change', not a routing artifact).
    """
    denied = build_denied_trusted_change_report_demo()
    accepted = build_trusted_change_report_demo()

    assert (
        denied["report"]["route_decision"]["selected_route"]["runtime_id"]
        == (accepted["report"]["route_decision"]["selected_route"]["runtime_id"])
    )
    # No boundary violations on the denied leg — denial is the gate, not scope.
    assert denied["report"]["diff_summary"]["boundary_violations"] == []


def test_denied_validator_fails_closed_on_drift(monkeypatch: object) -> None:
    """The denied validator raises if the leg ever drifts to accepted (drift guard)."""
    from verdict.flagship_demo import validate_trusted_change_report_denied_demo

    with pytest.raises(ValueError):  # type: ignore[name-defined]
        validate_trusted_change_report_denied_demo("accepted", "ACCEPTED_ALL_GATES_GREEN")
    with pytest.raises(ValueError):  # type: ignore[name-defined]
        validate_trusted_change_report_denied_demo("denied", "DENIED_UNBOUND_SOURCE")


def test_denied_redacted_export_is_leak_free() -> None:
    """The denied leg's portable export carries no credentials or producer-internal fields."""
    result = build_denied_trusted_change_report_demo()
    redacted = result["redacted"]

    assert redacted["acceptance"]["decision"] == "denied"
    assert redacted["acceptance"]["reason"] == "DENIED_FAILED_CHECK"
    for vr in redacted["verification_results"]:
        assert "raw_output" not in vr
        assert "command" not in vr
        assert "runtime" not in vr
    text = json.dumps(redacted)
    assert "OPENAI_API_KEY" not in text
    assert "sk-demo" not in text
    assert "git@" not in text
    assert "https://" not in text
    assert "http://" not in text
    assert "api_key" not in text.lower() or "no_api_key" in text.lower()


def test_run_accepted_and_denied_demo_returns_both_legs() -> None:
    """The combined runner proves gate 1: an accepted AND a denied change in one call."""
    both = run_accepted_and_denied_demo()

    assert set(both) == {"accepted", "denied"}
    assert both["accepted"]["verdict"]["decision"] == "accepted"
    assert both["accepted"]["verdict"]["reason"] == "ACCEPTED_ALL_GATES_GREEN"
    assert both["denied"]["verdict"]["decision"] == "denied"
    assert both["denied"]["verdict"]["reason"] == "DENIED_FAILED_CHECK"


def test_run_accepted_and_denied_demo_is_deterministic() -> None:
    """Both legs have stable canonical payloads and report digests across runs."""
    a = run_accepted_and_denied_demo()
    b = run_accepted_and_denied_demo()

    for leg in ("accepted", "denied"):
        first = TrustedChangeReport(**a[leg]["report"])
        second = TrustedChangeReport(**b[leg]["report"])
        assert canonical_report_payload(first) == canonical_report_payload(second)
        assert compute_report_digest(first) == compute_report_digest(second)
        assert a[leg]["redacted"] == b[leg]["redacted"]

    # The two legs' reports are distinct (different report id / source state id).
    assert a["accepted"]["report"]["report_id"] != a["denied"]["report"]["report_id"]


def test_installed_wheel_emits_both_legs_and_writes_no_files(tmp_path: Path) -> None:
    """The packaged demo runs without checkout imports, credentials, or side effects."""
    wheel_dir = tmp_path / "wheel"
    venv_dir = tmp_path / "venv"
    run_dir = tmp_path / "run"
    wheel_dir.mkdir()
    run_dir.mkdir()

    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir), str(ROOT)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_dir.glob("verdict_core-*.whl"))
    python = venv_dir / "bin" / "python"
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for name in ("PYTHONPATH", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLMGATE_UPSTREAM_API_KEY"):
        env.pop(name, None)
    result = subprocess.run(
        [str(python), "-I", "-m", "verdict.flagship_demo"],
        cwd=run_dir,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    payload = json.loads(result.stdout)
    assert payload["accepted"]["verdict"]["decision"] == "accepted"
    assert payload["denied"]["verdict"]["decision"] == "denied"
    assert payload["denied"]["verdict"]["reason"] == "DENIED_FAILED_CHECK"
    # No side effects in the clean-install working directory.
    assert list(run_dir.iterdir()) == []
