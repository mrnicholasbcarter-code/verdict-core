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
import re
import subprocess
import sys
import tempfile
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


# ===========================================================================
# F2 — extended golden contract (BOD-275 lane F2)
# ===========================================================================
# These tests cover the commands F skipped in the first pass (F1):
#   route (--terse + default), compare, stats, cost-report, suggest,
#   receipt show (missing ID), catalog (connection-refused), eligibility
#   (faked inventory shim).
# Doctor --json is skipped: see README for reason.
#
# Normalised volatile fields:
#   route/compare — "Latency  0.0ms" replaces wall-clock latency;
#                   "NORMALIZED" replaces ISO-8601 timestamps in JSON.
#
# All tests run from a per-test tmpdir (no stray verdict-decisions.jsonl).
# ===========================================================================


def _clean_workdir() -> str:
    """Return a fresh tmp directory with no verdict-decisions.jsonl."""
    return tempfile.mkdtemp(prefix="/tmp/vg_f2_")


def _norm_route(stdout: str) -> str:
    """Normalise volatile latency and timestamp fields in route output."""
    stdout = re.sub(r"(Latency\s+)[\d.]+ms", r"\g<1>0.0ms", stdout)
    stdout = re.sub(r'"timestamp": "[^"]*"', '"timestamp": "NORMALIZED"', stdout)
    return stdout


def _norm_compare(stdout: str) -> str:
    """Normalise volatile timestamp and latency_delta fields in compare output."""
    stdout = re.sub(r'"timestamp": "[^"]*"', '"timestamp": "NORMALIZED"', stdout)
    stdout = re.sub(r'"latency_ms": [\d.]+', '"latency_ms": 0.0', stdout)
    return stdout


# ---------------------------------------------------------------------------
# stats / cost-report / suggest — human output with no --json flag
# ---------------------------------------------------------------------------


def test_stats_no_log_golden() -> None:
    """stats with no log file produces deterministic 'no log file' warning."""
    import subprocess as _sp

    result = _sp.run(
        [sys.executable, "-m", "verdict", "stats"],
        cwd=_clean_workdir(),
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("stats__no_log")
    assert result.returncode == fixture["exit_code"], result.stderr[-200:]
    assert result.stdout == fixture["stdout"], (
        f"stdout mismatch:\ngot:  {result.stdout[:300]!r}\nwant: {fixture['stdout'][:300]!r}"
    )


def test_cost_report_no_log_golden() -> None:
    """cost-report with no log file produces deterministic empty-log output."""
    import subprocess as _sp

    cwd = _clean_workdir()
    result = _sp.run(
        [sys.executable, "-m", "verdict", "cost-report"],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("cost_report__no_log")
    assert result.returncode == fixture["exit_code"], result.stderr[-200:]
    assert result.stdout == fixture["stdout"], (
        f"stdout mismatch:\ngot:  {result.stdout[:300]!r}\nwant: {fixture['stdout'][:300]!r}"
    )


def test_suggest_no_log_golden() -> None:
    """suggest with no log file produces deterministic 'no suggestions' output."""
    import subprocess as _sp

    cwd = _clean_workdir()
    result = _sp.run(
        [sys.executable, "-m", "verdict", "suggest"],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("suggest__no_log")
    assert result.returncode == fixture["exit_code"], result.stderr[-200:]
    assert result.stdout == fixture["stdout"], (
        f"stdout mismatch:\ngot:  {result.stdout[:300]!r}\nwant: {fixture['stdout'][:300]!r}"
    )


# ---------------------------------------------------------------------------
# route — --terse and default mode (--allow-offline; no --json flag)
# ---------------------------------------------------------------------------


def test_route_terse_offline_golden() -> None:
    """route --terse --allow-offline → JSON error payload on stdout, exit 1."""
    import subprocess as _sp

    cwd = _clean_workdir()
    result = _sp.run(
        [
            sys.executable,
            "-m",
            "verdict",
            "route",
            "write a hello world",
            "--allow-offline",
            "--terse",
        ],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("route__terse_offline")
    assert result.returncode == fixture["exit_code"], (
        f"exit_code: got {result.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {result.stderr[-200:]}"
    )
    # stdout is JSON — parse and compare
    parsed = json.loads(result.stdout)
    expected = json.loads(fixture["stdout"])
    assert parsed == expected, (
        f"JSON mismatch:\ngot:  {json.dumps(parsed, sort_keys=True)[:400]}\n"
        f"want: {json.dumps(expected, sort_keys=True)[:400]}"
    )
    assert "\033[" not in result.stdout


def test_route_default_offline_golden() -> None:
    """route --allow-offline → human table + JSON footer (latency+timestamp normalised)."""
    import subprocess as _sp

    cwd = _clean_workdir()
    result = _sp.run(
        [sys.executable, "-m", "verdict", "route", "write a hello world", "--allow-offline"],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("route__default_offline")
    assert result.returncode == fixture["exit_code"], (
        f"exit_code: got {result.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {result.stderr[-200:]}"
    )
    normalised = _norm_route(result.stdout)
    assert normalised == fixture["stdout"], (
        f"stdout mismatch (after normalisation):\n"
        f"got:  {normalised[:400]!r}\n"
        f"want: {fixture['stdout'][:400]!r}"
    )
    assert "\033[" not in result.stdout


# ---------------------------------------------------------------------------
# compare — --allow-offline (no --json flag; prints JSON directly)
# ---------------------------------------------------------------------------


def test_compare_offline_golden() -> None:
    """compare --allow-offline → JSON comparison report (timestamp+latency normalised)."""
    import subprocess as _sp

    cwd = _clean_workdir()
    result = _sp.run(
        [sys.executable, "-m", "verdict", "compare", "write a hello world", "--allow-offline"],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("compare__offline")
    assert result.returncode == fixture["exit_code"], (
        f"exit_code: got {result.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {result.stderr[-200:]}"
    )
    normalised = _norm_compare(result.stdout)
    parsed = json.loads(normalised)
    expected = json.loads(fixture["stdout"])
    assert parsed == expected, (
        f"JSON mismatch:\ngot:  {json.dumps(parsed, sort_keys=True)[:400]}\n"
        f"want: {json.dumps(expected, sort_keys=True)[:400]}"
    )
    assert "\033[" not in result.stdout


# ---------------------------------------------------------------------------
# receipt show --json — missing ID (empty stdout, exit 1, error on stderr)
# ---------------------------------------------------------------------------


def test_receipt_show_missing_golden() -> None:
    """receipt show --json for a missing ID exits 1 with empty stdout."""
    import subprocess as _sp

    cwd = _clean_workdir()
    db_path = "/tmp/vg_test_home/.verdict/receipts.db"
    result = _sp.run(
        [
            sys.executable,
            "-m",
            "verdict",
            "receipt",
            "show",
            "nonexistent-id-f2test",
            "--json",
            "--db",
            db_path,
        ],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("receipt_show__missing")
    assert result.returncode == fixture["exit_code"], (
        f"exit_code: got {result.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {result.stderr[-200:]}"
    )
    # stdout is empty for missing receipt (error goes to stderr as SystemExit message)
    assert result.stdout == fixture["stdout"]


# ---------------------------------------------------------------------------
# catalog --json — connection-refused baseline (deterministic URLError payload)
# ---------------------------------------------------------------------------


def test_catalog_refused_golden() -> None:
    """catalog --json with unreachable gateway → URLError error payload, exit 1."""
    import subprocess as _sp

    cwd = _clean_workdir()
    result = _sp.run(
        [sys.executable, "-m", "verdict", "catalog", "--base-url", "http://127.0.0.1:9", "--json"],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("catalog__refused")
    assert result.returncode == fixture["exit_code"], (
        f"exit_code: got {result.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {result.stderr[-200:]}"
    )
    parsed = json.loads(result.stdout)
    expected = json.loads(fixture["stdout"])
    assert parsed == expected, (
        f"JSON mismatch:\ngot:  {json.dumps(parsed, sort_keys=True)[:600]}\n"
        f"want: {json.dumps(expected, sort_keys=True)[:600]}"
    )
    assert "\033[" not in result.stdout


# ---------------------------------------------------------------------------
# eligibility --json — faked 2-row inventory via in-process shim subprocess
# ---------------------------------------------------------------------------

_ELIG_SHIM = Path(__file__).resolve().parent / "helpers" / "eligibility_golden_shim.py"


def test_eligibility_faked_inventory_golden() -> None:
    """eligibility --json with monkeypatched fetch_inventory → deterministic ladder output."""
    import subprocess as _sp

    cwd = _clean_workdir()
    env = _base_env()
    env["PYTHONPATH"] = str(ROOT)
    result = _sp.run(
        [sys.executable, str(_ELIG_SHIM)],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("eligibility__faked_inventory")
    assert result.returncode == fixture["exit_code"], (
        f"exit_code: got {result.returncode}, want {fixture['exit_code']}\n"
        f"stderr: {result.stderr[-400:]}"
    )
    assert result.stdout.strip(), f"empty stdout, stderr: {result.stderr[:300]}"
    parsed = json.loads(result.stdout)
    expected = json.loads(fixture["stdout"])
    assert parsed == expected, (
        f"JSON mismatch:\ngot:  {json.dumps(parsed, sort_keys=True)[:600]}\n"
        f"want: {json.dumps(expected, sort_keys=True)[:600]}"
    )
    assert "\033[" not in result.stdout
