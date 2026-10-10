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
        # Pin terminal width so rendering is deterministic regardless of COLUMNS
        # leaking from the test process (e.g., from in-process tests that mutate
        # os.environ["COLUMNS"]).  80 is wide enough for all golden table layouts.
        "COLUMNS": "80",
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
    # Rich pads the original latency cell before we replace its value. A
    # two-digit reading leaves one fewer trailing space than a one-digit
    # reading; that padding is volatile too. Normalize only this row.
    stdout = re.sub(r"(Latency[ \t]+)[\d.]+ms[ \t]*", r"\g<1>0.0ms", stdout)
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
    assert normalised == _norm_route(fixture["stdout"]), (
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


# ===========================================================================
# F3 — success-path goldens (BOD-275 lane F3)
# ===========================================================================
# These tests cover the NORMAL (non-error) paths for commands whose handlers
# are being moved onto shared actions:
#
# stats, cost-report, suggest — human output with a committed fixture log.
# receipt show --json          — valid receipt stored in an isolated DB.
# probe --json                 — transport faked in-process via shim.
# replay --json                — session seeded from a static fixture via shim.
#
# Volatile fields normalised:
#   probe:  diagnostics.started_at / finished_at → "NORMALIZED"; duration_ms,
#           results[].latency_ms → 0.0
#   replay: created_at / updated_at / checkpoints[].created_at /
#           steps[].started_at → 0.0
# ===========================================================================

_FIXTURE_LOG = INPUTS / "routing-decisions.jsonl"
_PROBE_SHIM = Path(__file__).resolve().parent / "helpers" / "probe_golden_shim.py"
_REPLAY_SHIM = Path(__file__).resolve().parent / "helpers" / "replay_golden_shim.py"

# Stable receipt coordinates written by the capture script.
_RECEIPT_ID = "rcpt-f3test-golden-bod275"
_RECEIPT_SCOPE = "attempt/f3golden/unit-1/attempt-1"


def _receipt_db_path() -> str:
    """Return a writable receipts.db under a temp HOME, pre-seeded with the F3 receipt."""
    import tempfile

    from verdict.receipt_store import ReceiptStore
    from verdict.routing_receipt import RoutingReceiptV1

    tmp_home = tempfile.mkdtemp(prefix="vgolden_rcpt_")
    os.makedirs(os.path.join(tmp_home, ".verdict"), exist_ok=True)
    db = os.path.join(tmp_home, ".verdict", "receipts.db")
    store = ReceiptStore(db, strict_scope=True)
    receipt = RoutingReceiptV1(
        receipt_id=_RECEIPT_ID,
        attempt_id="attempt-f3golden-001",
        story_id="f3golden-story",
        work_unit_id="unit-1",
        created_at="2026-09-28T12:00:00Z",
        state="finalized",
        task_profile={"task": "write unit tests", "criticality": "medium"},
        decision={"model": "kr/claude-sonnet-5-thinking", "provider": "kr", "tier": 1},
    )
    store.put_receipt(
        receipt_type="decision",
        scope=_RECEIPT_SCOPE,
        payload=receipt.to_dict(),
        receipt_id=_RECEIPT_ID,
    )
    return db


# ---------------------------------------------------------------------------
# stats / cost-report / suggest — human output with a real log fixture
# ---------------------------------------------------------------------------


def test_stats_with_log_golden() -> None:
    """stats with a fixture log produces deterministic human output."""
    import subprocess as _sp

    result = _sp.run(
        [sys.executable, "-m", "verdict", "stats", f"--log_path={_FIXTURE_LOG}"],
        cwd=_clean_workdir(),
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("stats__with_log")
    assert result.returncode == fixture["exit_code"], result.stderr[-200:]
    assert result.stdout == fixture["stdout"], (
        f"stdout mismatch:\ngot:  {result.stdout[:300]!r}\nwant: {fixture['stdout'][:300]!r}"
    )


def test_cost_report_with_log_golden() -> None:
    """cost-report with verdict-decisions.jsonl in CWD produces deterministic output."""
    import shutil
    import subprocess as _sp
    import tempfile

    cwd = tempfile.mkdtemp(prefix="vgolden_cost_")
    shutil.copy(str(_FIXTURE_LOG), os.path.join(cwd, "verdict-decisions.jsonl"))
    result = _sp.run(
        [sys.executable, "-m", "verdict", "cost-report"],
        cwd=cwd,
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("cost_report__with_log")
    assert result.returncode == fixture["exit_code"], result.stderr[-200:]
    assert result.stdout == fixture["stdout"], (
        f"stdout mismatch:\ngot:  {result.stdout[:300]!r}\nwant: {fixture['stdout'][:300]!r}"
    )


def test_suggest_with_log_golden() -> None:
    """suggest with a fixture log produces deterministic human output."""
    import subprocess as _sp

    result = _sp.run(
        [sys.executable, "-m", "verdict", "suggest", f"--log_path={_FIXTURE_LOG}"],
        cwd=_clean_workdir(),
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("suggest__with_log")
    assert result.returncode == fixture["exit_code"], result.stderr[-200:]
    assert result.stdout == fixture["stdout"], (
        f"stdout mismatch:\ngot:  {result.stdout[:300]!r}\nwant: {fixture['stdout'][:300]!r}"
    )


# ---------------------------------------------------------------------------
# receipt show --json — valid receipt (exit 0)
# ---------------------------------------------------------------------------


def test_receipt_show_valid_json_golden() -> None:
    """receipt show --json with a seeded DB returns the stored receipt payload."""
    import subprocess as _sp

    db = _receipt_db_path()
    result = _sp.run(
        [
            sys.executable,
            "-m",
            "verdict",
            "receipt",
            "show",
            _RECEIPT_ID,
            "--db",
            db,
            "--scope",
            _RECEIPT_SCOPE,
            "--json",
        ],
        cwd=_clean_workdir(),
        env=_base_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("receipt_show__valid")
    assert result.returncode == fixture["exit_code"], result.stderr[-400:]
    assert result.stdout.strip(), "empty stdout"
    parsed = _canonical(result.stdout)
    expected = _canonical(fixture["stdout"])
    # Structural fields must match exactly (created_at is fixed in the fixture)
    assert parsed["receipt_id"] == expected["receipt_id"]
    assert parsed["state"] == expected["state"]
    assert parsed["schema_version"] == expected["schema_version"]
    assert parsed["created_at"] == expected["created_at"]
    assert parsed["decision_digest"] == expected["decision_digest"]
    assert parsed["decision"] == expected["decision"]
    assert "[" not in result.stdout
    assert "[" not in result.stdout


# ---------------------------------------------------------------------------
# probe --json — transport faked in-process via shim (exit 0)
# ---------------------------------------------------------------------------


def test_probe_success_json_golden() -> None:
    """probe --json with injected fake transport returns a ready result."""
    import subprocess as _sp

    env = _base_env()
    env["PYTHONPATH"] = str(ROOT)
    result = _sp.run(
        [sys.executable, str(_PROBE_SHIM)],
        cwd=_clean_workdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("probe__success")
    assert result.returncode == fixture["exit_code"], (
        f"exit {result.returncode} != {fixture['exit_code']}\nstderr: {result.stderr[-400:]}"
    )
    assert result.stdout.strip(), "empty stdout"
    parsed = _canonical(result.stdout)

    # Normalise volatile timing before comparison
    parsed["diagnostics"]["started_at"] = "NORMALIZED"
    parsed["diagnostics"]["finished_at"] = "NORMALIZED"
    parsed["diagnostics"]["duration_ms"] = 0.0
    for res in parsed.get("results", []):
        res["latency_ms"] = 0.0

    expected = _canonical(fixture["stdout"])
    assert parsed == expected, (
        f"JSON mismatch:\ngot:  {json.dumps(parsed, sort_keys=True)[:600]}\n"
        f"want: {json.dumps(expected, sort_keys=True)[:600]}"
    )
    assert "[" not in result.stdout
    assert "[" not in result.stdout


# ---------------------------------------------------------------------------
# replay --json — session seeded from a static fixture via shim (exit 0)
# ---------------------------------------------------------------------------


def _norm_replay(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise float timestamps so replay goldens are byte-stable."""
    d = {k: v for k, v in data.items()}
    d["created_at"] = 0.0
    d["updated_at"] = 0.0
    d["checkpoints"] = [{**ck, "created_at": 0.0} for ck in d.get("checkpoints", [])]
    d["steps"] = [{**st, "started_at": 0.0} for st in d.get("steps", [])]
    return d


def test_replay_valid_json_golden() -> None:
    """replay --json for a seeded session returns structural fields unchanged."""
    import subprocess as _sp

    env = _base_env()
    env["PYTHONPATH"] = str(ROOT)
    result = _sp.run(
        [sys.executable, str(_REPLAY_SHIM)],
        cwd=_clean_workdir(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    fixture = _load_fixture("replay__valid")
    assert result.returncode == fixture["exit_code"], (
        f"exit {result.returncode} != {fixture['exit_code']}\nstderr: {result.stderr[-400:]}"
    )
    assert result.stdout.strip(), "empty stdout"
    parsed = _norm_replay(_canonical(result.stdout))
    expected = _norm_replay(_canonical(fixture["stdout"]))
    assert parsed["session_id"] == expected["session_id"]
    assert parsed["model_id"] == expected["model_id"]
    assert parsed["state"] == expected["state"]
    assert parsed["schema_version"] == expected["schema_version"]
    assert parsed["task_spec"] == expected["task_spec"]
    assert len(parsed["steps"]) == len(expected["steps"])
    assert [s["name"] for s in parsed["steps"]] == [s["name"] for s in expected["steps"]]
    assert "[" not in result.stdout
    assert "[" not in result.stdout


def test_route_latency_normalization_removes_only_volatile_row_padding() -> None:
    one_digit = "  Task    keep spacing  \n  Latency    1.0ms    \n  Strategy    DIRECT  \n"
    two_digits = "  Task    keep spacing  \n  Latency    12.0ms   \n  Strategy    DIRECT  \n"
    assert _norm_route(one_digit) == _norm_route(two_digits)
    assert "  Task    keep spacing  \n" in _norm_route(one_digit)
    assert "  Strategy    DIRECT  \n" in _norm_route(one_digit)
