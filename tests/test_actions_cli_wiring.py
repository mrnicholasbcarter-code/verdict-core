"""Lane B (BOD-275): CLI handlers wired through run_action + byte-equal contract.

For every handler this lane owns we assert two things:

1. Wired: the CLI handler reaches ``run_action(<action>, ...)`` in verdict.actions.
2. Byte-equal: --json and human stdout are unchanged compared to fixed-input
   baselines captured before the rewire.  Baselines live in
   ``tests/fixtures/actions_cli_wiring_baselines/`` alongside the input logs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures" / "actions_cli_wiring_baselines"
BASELINES = FIX / "baselines"


# ``*.jsonl`` is gitignored, so tests materialize the log fixture on demand.
_LOG_LINES = (
    '{"model":"gpt-4o","effective_tier":0,"latency_ms":100.0,"session_id":"s1","cost":0.001}\n'
    '{"model":"gpt-4o-mini","effective_tier":2,"latency_ms":80.0,"session_id":"s1","cost":0.0005}\n'
    '{"model":"claude-3","effective_tier":1,"latency_ms":120.0,"session_id":"s2","cost":0.002}\n'
    '{"decision":{"model":"gpt-4o","tier":0,"latency_ms":90.0},"session_id":"s3","cost":0.0015}\n'
)


@pytest.fixture(scope="module", autouse=True)
def _materialize_logs() -> None:
    """Recreate the gitignored ``*.jsonl`` inputs the byte-equal tests read."""
    (FIX / "decisions.jsonl").write_text(_LOG_LINES)
    (FIX / "cwd_costreport").mkdir(exist_ok=True)
    (FIX / "cwd_costreport" / "verdict-decisions.jsonl").write_text(_LOG_LINES)
    (FIX / "cwd_empty").mkdir(exist_ok=True)
    (FIX / "runs" / "run-fake").mkdir(parents=True, exist_ok=True)
    (FIX / "runs" / "run-fake" / "events.jsonl").write_text("")


LOG_JSONL = FIX / "decisions.jsonl"
MISSING = FIX / "no-such-log.jsonl"
# Relative form for byte-equal CLI invocations (cwd == FIX): keeps the rendered
# "No log file found at ..." message short so Rich never soft-wraps it across a
# narrow CI terminal width, which would otherwise split the path mid-string and
# break the <FIXROOT> normalization (observed failure: CI's longer absolute
# checkout path wrapped where this repo's shorter dev path did not).
MISSING_REL = "no-such-log.jsonl"

# Deterministic scrubs for values that vary between runs (timestamps, latency).
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)")
# Trailing ``[ \t]*`` absorbs the fixed-width table's padding after the ms
# value, since a wider latency value (e.g. "12.4ms" vs "2.4ms") shifts that
# padding by the same number of characters it grew (table rows pad to a
# constant width). Without this the normalized row length differs whenever
# subprocess latency crosses a digit-count boundary (BOD flaky-test report).
_LAT_RE = re.compile(r"Latency\s+\d+(?:\.\d+)?ms[ \t]*")


def _normalize(text: str) -> str:
    text = _ISO_RE.sub("<ISO>", text)
    text = _LAT_RE.sub("Latency N.Nms", text)
    text = text.replace(str(FIX), "<FIXROOT>")
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
    return e


def _run(argv: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "verdict", *argv],
        cwd=str(cwd),
        env=_env(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, proc.stdout


# ---------------------------------------------------------------------------
# 1. Wired: every handler in this lane calls verdict.actions.registry.run_action.
# ---------------------------------------------------------------------------

_WIRED_CASES = [
    # (handler, expected action name, minimal kwargs to reach run_action)
    (
        "verdict.cli",
        "cmd_route",
        "route",
        {"task": "t", "criticality": "low", "terse": True, "allow_offline": True},
    ),
    ("verdict.cli", "cmd_compare", "compare", {"task": "t", "allow_offline": True}),
    ("verdict.cli", "cmd_stats", "stats", {"log_path": str(MISSING)}),
    ("verdict.cli", "cmd_cost_report", "cost-report", {}),
    ("verdict.cli", "cmd_suggest", "suggest", {"log_path": str(MISSING)}),
    ("verdict.cli", "cmd_replay", "replay", {"session_id": "no-such"}),
    ("verdict.cli", "cmd_detect", "detect", {"offline": True, "output_json": True}),
    ("verdict.cli", "cmd_receipt", "receipt.show", {"action": "list", "output_json": True}),
]


@pytest.mark.parametrize(
    "module,name,action,kwargs", _WIRED_CASES, ids=[c[1] for c in _WIRED_CASES]
)
def test_handler_calls_run_action(
    module: str, name: str, action: str, kwargs: dict[str, object]
) -> None:
    """The handler must reach run_action with the expected action name."""
    import importlib

    from verdict.actions.registry import get_action

    mod = importlib.import_module(module)
    handler = getattr(mod, name)
    real = get_action(action)
    assert real is not None, f"action {action!r} not registered"
    with patch(
        "verdict.actions.registry.run_action",
        wraps=__import__("verdict.actions.registry", fromlist=["run_action"]).run_action,
    ) as spy:
        import contextlib

        with contextlib.suppress(SystemExit):
            handler(**kwargs)
        called_actions = [call.args[0] for call in spy.call_args_list]
        assert action in called_actions, (
            f"{name} did not call run_action({action!r}); called: {called_actions}"
        )


def test_catalog_handler_calls_run_action() -> None:
    """cmd_catalog reaches run_action('catalog') even on the refused path (exit 2)."""
    from verdict.actions.registry import run_action as real
    from verdict.cli import cmd_catalog

    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        with pytest.raises(SystemExit):
            cmd_catalog(
                base_url="http://127.0.0.1:9",
                management=False,
                expected_rows=0,
                freshness_seconds=0,
                db_path=None,
                probe=True,
                probe_limit=1,
                probe_timeout=1.0,
                output_json=True,
                allow_live_probe=False,
            )
        assert any(c.args[0] == "catalog" for c in spy.call_args_list)


def test_orchestration_eligibility_wired() -> None:
    """orchestration/cli.py _eligibility calls run_action('eligibility')."""
    import argparse

    from verdict.actions.registry import run_action as real
    from verdict.orchestration.cli import _eligibility

    args = argparse.Namespace(
        gateway="http://127.0.0.1:9",
        scope="",
        prefer="claude",
        provider_family=[],
        reasoning=False,
        frontier=False,
        probe=False,
        json=True,
        no_pager=True,
    )
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        import contextlib

        with contextlib.suppress(Exception):
            _eligibility(args)  # gateway is unreachable in the test env
        assert any(c.args[0] == "eligibility" for c in spy.call_args_list)


def test_orchestration_run_receipt_wired(tmp_path: Path) -> None:
    """orchestration/cli.py _receipt (run-receipt) calls run_action('run-receipt')."""
    import argparse

    from verdict.actions.registry import run_action as real
    from verdict.orchestration.cli import _receipt

    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    (run_dir / "receipt.json").write_text(json.dumps({"run_id": "run-x", "nodes": []}))
    (run_dir / "events.jsonl").write_text("")
    args = argparse.Namespace(run=str(run_dir), runs_dir=str(tmp_path), json=True)
    with patch("verdict.actions.registry.run_action", wraps=real) as spy:
        _receipt(args)
        assert any(c.args[0] == "run-receipt" for c in spy.call_args_list)


# ---------------------------------------------------------------------------
# 2. Byte-equal: stdout matches the captured baselines for each handler.
# ---------------------------------------------------------------------------

_BYTE_EQUAL_CASES = [
    # (baseline name, argv, cwd relative to FIX)
    ("stats_missing", ["stats", "--log_path", MISSING_REL], "."),
    ("stats_present", ["stats", "--log_path", str(LOG_JSONL)], "."),
    ("costreport_present", ["cost-report"], "cwd_costreport"),
    ("costreport_missing", ["cost-report"], "cwd_empty"),
    ("suggest_missing", ["suggest", "--log_path", MISSING_REL], "."),
    ("suggest_present", ["suggest", "--log_path", str(LOG_JSONL)], "."),
    ("replay_missing", ["replay", "unknown-session"], "."),
    ("replay_json", ["replay", "unknown-session", "--json"], "."),
    ("detect_offline", ["detect", "--offline"], "."),
    ("detect_offline_json", ["detect", "--offline", "--json"], "."),
    ("catalog_refused", ["catalog", "--probe", "--json"], "."),
    (
        "compare_offline",
        ["compare", "sum two ints", "--criticality", "low", "--allow-offline"],
        ".",
    ),
]


@pytest.mark.parametrize(
    "name,argv,rel_cwd", _BYTE_EQUAL_CASES, ids=[c[0] for c in _BYTE_EQUAL_CASES]
)
def test_stdout_is_byte_equal(name: str, argv: list[str], rel_cwd: str, tmp_path: Path) -> None:
    """Baseline stdout must be preserved byte-for-byte (after FIXROOT normalization)."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = FIX / rel_cwd
    rc, out = _run(argv, cwd)
    expected = (BASELINES / f"{name}.out").read_text()
    exp_rc = int((BASELINES / f"{name}.rc").read_text())
    # Baselines store the FIX absolute path as <FIXROOT> so tests are
    # worktree-independent (issue: PR-branch worktrees embed /tmp/v275b*).
    assert rc == exp_rc, f"exit code drift for {name}"
    assert _normalize(out) == _normalize(expected), f"stdout drift for {name}"


def test_route_offline_stdout_matches_after_isotimestamp_normalization(tmp_path: Path) -> None:
    """route --allow-offline uses now(); timestamps + latency vary but structure holds."""
    home = tmp_path / "home"
    home.mkdir()
    rc, out = _run(["route", "sum two ints", "--criticality", "low", "--allow-offline"], FIX)
    exp_out = (BASELINES / "route_offline.out").read_text()
    exp_rc = int((BASELINES / "route_offline.rc").read_text())
    assert rc == exp_rc
    assert _normalize(out) == _normalize(exp_out)


def test_route_offline_latency_padding_normalizes_across_digit_widths() -> None:
    """Regression for the flaky latency-width bug.

    The table renders fixed-width rows: a wider latency value (e.g. two or
    three digits before the decimal point vs. one) shifts the row's trailing
    padding left by exactly the number of characters the value grew, so the
    row stays the same total width. ``_LAT_RE`` must absorb that padding
    (not just the digits) or ``_normalize`` leaves a residual whitespace
    difference between runs where the real subprocess happens to report a
    latency >= 10ms and the 2.4ms baseline.

    This rewrites the real baseline's Latency row to 12.4ms and 123.4ms
    (padding shortened to preserve the row's fixed width) and asserts both
    normalize identically to the original baseline.
    """
    baseline = (BASELINES / "route_offline.out").read_text()
    lines = baseline.split("\n")
    lat_idx = next(i for i, ln in enumerate(lines) if ln.startswith("  Latency"))
    lat_line = lines[lat_idx]
    row_width = len(lat_line)
    match = re.search(r"\d+(?:\.\d+)?ms", lat_line)
    assert match is not None, "baseline Latency row has no ms value to rewrite"
    prefix = lat_line[: match.start()]

    def _row_with_latency(value: str) -> str:
        pad_len = row_width - len(prefix) - len(value)
        assert pad_len >= 0, f"{value!r} is wider than the fixed row width"
        return prefix + value + " " * pad_len

    for value in ("12.4ms", "123.4ms"):
        variant_lines = list(lines)
        variant_lines[lat_idx] = _row_with_latency(value)
        variant = "\n".join(variant_lines)
        assert len(variant_lines[lat_idx]) == row_width
        assert _normalize(variant) == _normalize(baseline), f"normalize drift for Latency={value!r}"


def test_route_offline_terse_stdout_is_byte_equal(tmp_path: Path) -> None:
    """route --terse --allow-offline is fully deterministic (no timestamps)."""
    rc, out = _run(
        ["route", "sum two ints", "--criticality", "low", "--terse", "--allow-offline"], FIX
    )
    exp_out = (BASELINES / "route_offline_terse.out").read_text()
    exp_rc = int((BASELINES / "route_offline_terse.rc").read_text())
    assert rc == exp_rc
    assert json.loads(out) == json.loads(exp_out)


def test_run_receipt_stdout_matches(tmp_path: Path) -> None:
    """orchestration run-receipt handler stdout is preserved."""
    # Rebuild the fake receipt fixture (paths absolute in the baseline).
    run_dir = FIX / "runs" / "run-fake"
    if not run_dir.exists():
        run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "run-fake",
                "goal": "demo",
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": "2026-01-01T00:00:00Z",
                "events_digest": "sha256:0000",
                "nodes": [],
                "review": {"status": "N/A", "reviewer": None, "route_id": None},
                "outcome": "COMPLETE",
            }
        )
    )
    (run_dir / "events.jsonl").write_text("")
    for name, argv in (
        ("run_receipt_json", ["run-receipt", str(run_dir), "--json"]),
        ("run_receipt_human", ["run-receipt", str(run_dir)]),
    ):
        rc, out = _run(argv, FIX)
        exp_out = (BASELINES / f"{name}.out").read_text()
        exp_rc = int((BASELINES / f"{name}.rc").read_text())
        assert rc == exp_rc, name
        assert _normalize(out) == _normalize(exp_out), name


# ---------------------------------------------------------------------------
# 3. No business logic left in handlers: every rewired body reaches run_action.
# ---------------------------------------------------------------------------

_REWIRED_HANDLERS = {
    "cmd_route",
    "cmd_compare",
    "cmd_stats",
    "cmd_cost_report",
    "cmd_suggest",
    "cmd_detect",
    "cmd_catalog",
    "cmd_receipt",
    "cmd_replay",
}


def test_handlers_import_run_action_lazily() -> None:
    """Each rewired handler imports run_action locally (never at module top)."""
    import inspect

    from verdict import cli

    for name in _REWIRED_HANDLERS:
        fn = getattr(cli, name)
        src = inspect.getsource(fn)
        assert "run_action" in src, f"{name} must call run_action"
        assert "from verdict.actions.registry import run_action" in src, (
            f"{name} must import run_action lazily inside the function body"
        )


def test_registry_has_no_reverse_import_for_this_lane() -> None:
    """The registry module (owned by this lane for its actions) must not
    import verdict.cli or verdict.commands. helpers.py belongs to lane A."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "verdict" / "actions" / "registry.py").read_text()
    assert "from verdict.cli" not in src, "registry.py imports verdict.cli"
    assert "import verdict.cli" not in src, "registry.py imports verdict.cli"
    assert "from verdict.commands" not in src, "registry.py imports verdict.commands"
    assert "import verdict.commands" not in src, "registry.py imports verdict.commands"
