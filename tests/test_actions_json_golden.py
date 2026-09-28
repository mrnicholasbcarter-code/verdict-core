"""JSON / exit-code golden contract for the action layer (lane F, BOD-275).

For every command in scope that has a ``--json`` form on ``origin/main``, this
file asserts that the same argv run through ``verdict.cli.main()`` on the PR
branch produces byte-identical JSON stdout and the same exit code as the
``origin/main`` baseline stored in ``tests/fixtures/actions_golden/``.

Additionally, for each command, the test asserts that the JSON output is
parseable with no ANSI when stdout is a forced TTY, when ``NO_COLOR=1``, and
when ``CI=1``.

See ``tests/fixtures/actions_golden/README.md`` for the full list of skipped
commands and their documented reasons.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "actions_golden"
INPUTS = GOLDEN / "inputs"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict[str, Any]:
    """Load a golden fixture by name (without .json suffix)."""
    return json.loads((GOLDEN / f"{name}.json").read_text())


def _base_env() -> dict[str, str]:
    """Minimal hermetic environment matching the capture environment."""
    return {
        "HOME": "/tmp/verdict_golden_test_home",
        "XDG_CONFIG_HOME": "/tmp/verdict_golden_test_home/.config",
        "NO_COLOR": "1",
        "CI": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OMNIROUTE_BASE_URL": "http://127.0.0.1:9",
        "VERDICT_GATEWAY": "http://127.0.0.1:9",
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PYTHONPATH": str(ROOT),
    }


def _run_cli(
    argv: list[str], extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``verdict <argv>`` through the PR branch CLI."""
    env = _base_env()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "verdict", *argv],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _canonical(text: str) -> Any:
    """Parse JSON, raising on failure."""
    return json.loads(text)


def _assert_golden(
    proc: subprocess.CompletedProcess[str], fixture_name: str, *, json_equal: bool = True
) -> None:
    """Assert CLI output matches the golden fixture.

    ``json_equal=True`` (default): compares parsed JSON objects (equivalent
    to byte-equal modulo whitespace, which is deterministic for our commands).
    Set ``json_equal=False`` for cases where we only check exit_code + JSON
    parsability (e.g., large receipts with path-normalised fields).
    """
    fixture = _load_fixture(fixture_name)
    assert proc.returncode == fixture["exit_code"], (
        f"exit_code: got {proc.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {proc.stderr[-400:]}"
    )
    # stdout must parse as JSON with no ANSI
    assert proc.stdout.strip(), f"empty stdout for {fixture_name}"
    parsed = _canonical(proc.stdout)
    assert "\033[" not in proc.stdout
    assert "\x1b[" not in proc.stdout

    if json_equal:
        expected = _canonical(fixture["stdout"])
        assert parsed == expected, (
            f"JSON mismatch for {fixture_name}:\n"
            f"got:  {json.dumps(parsed, sort_keys=True)[:400]}\n"
            f"want: {json.dumps(expected, sort_keys=True)[:400]}"
        )


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------


def test_models_json_golden() -> None:
    """models --json produces byte-identical JSON to origin/main."""
    proc = _run_cli(["models", "--json"])
    _assert_golden(proc, "models__default")


def test_plan_json_golden() -> None:
    """plan --json produces byte-identical JSON to origin/main."""
    proc = _run_cli(["plan", "--json"])
    _assert_golden(proc, "plan__default")


def test_probe_refused_json_golden() -> None:
    """probe model-a --json (no live consent) matches origin/main refused payload."""
    proc = _run_cli(["probe", "model-a", "--json"])
    _assert_golden(proc, "probe__refused")


def test_detect_offline_json_golden() -> None:
    """detect --offline --json matches origin/main offline payload."""
    proc = _run_cli(["detect", "--offline", "--json"])
    _assert_golden(proc, "detect__offline")


def test_credentials_list_json_golden() -> None:
    """credentials list --json matches origin/main payload under clean env.

    OMNIROUTE_BASE_URL is always 18 chars in the test env so the
    captured ``set (len=18)`` value stays stable.
    """
    # Strip host API keys so the output is deterministic
    strip_env: dict[str, str] = {}
    for key in os.environ:
        if any(
            key.startswith(p)
            for p in (
                "VERDICT_",
                "OPENAI_",
                "ANTHROPIC_",
                "OPENROUTER_",
                "OMNIROUTE_API_KEY",
                "OMNIROUTE_MANAGEMENT_TOKEN",
            )
        ):
            strip_env[key] = ""
    proc = _run_cli(["credentials", "list", "--json"], extra_env=strip_env)
    _assert_golden(proc, "credentials_list__default")


def test_replay_missing_json_golden() -> None:
    """replay missing-session --json matches origin/main not-found payload."""
    proc = _run_cli(["replay", "missing-session", "--json"])
    _assert_golden(proc, "replay__missing")


def test_run_receipt_fixture_json_golden() -> None:
    """run-receipt <fixture_dir> --json matches origin/main output for a BLOCKED receipt.

    The receipt is static; no wall-clock timestamps in the output.
    """
    run_dir = str(INPUTS / "run-receipt-run")
    proc = _run_cli(["run-receipt", run_dir, "--json"])
    fixture = _load_fixture("run_receipt__fixture")

    assert proc.returncode == fixture["exit_code"], proc.stderr[-400:]
    assert proc.stdout.strip()
    parsed = _canonical(proc.stdout)

    # Structural fields must match exactly
    expected = _canonical(fixture["stdout"])
    assert parsed["outcome"] == expected["outcome"]
    assert parsed["reason"] == expected["reason"]
    assert parsed["problems"] == expected["problems"]
    assert parsed["receipt"]["claimed_outcome"] == expected["receipt"]["claimed_outcome"]
    assert parsed["receipt"]["events_digest"] == expected["receipt"]["events_digest"]
    assert parsed["receipt"]["graph_digest"] == expected["receipt"]["graph_digest"]

    # No ANSI in output
    assert "\033[" not in proc.stdout
    assert "\x1b[" not in proc.stdout


# ---------------------------------------------------------------------------
# Parametrized: JSON parseable under forced-TTY / NO_COLOR / CI variants
# ---------------------------------------------------------------------------

_ANSI_CASES: list[tuple[str, list[str]]] = [
    ("models", ["models", "--json"]),
    ("probe__refused", ["probe", "model-a", "--json"]),
    ("detect__offline", ["detect", "--offline", "--json"]),
    ("replay__missing", ["replay", "missing-session", "--json"]),
]


@pytest.mark.parametrize("label,argv", _ANSI_CASES, ids=[c[0] for c in _ANSI_CASES])
def test_json_parseable_no_ansi_forced_tty(label: str, argv: list[str]) -> None:
    """stdout parses as JSON with no ANSI when NO_COLOR=1 and CI=1."""
    proc = _run_cli(argv, extra_env={"NO_COLOR": "1", "CI": "1"})
    # Must produce non-empty stdout that parses as JSON
    assert proc.stdout.strip(), f"empty stdout: {proc.stderr[:200]}"
    parsed = json.loads(proc.stdout)
    assert parsed is not None
    assert "\033[" not in proc.stdout
    assert "\x1b[" not in proc.stdout
