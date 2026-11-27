"""Run-scoped exclusion and cooldown on the eligibility ladder (GitHub issue #1366).

A ``verdict serve`` denial must not outlive the run: the ladder holds the exclusion
and the cooldown in memory and writes neither to its health state file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_orch_eligibility import (  # noqa: F401  (the fixture declares the fake providers)
    _subscription_overlay,
    by_route,
    conn,
    make_ladder,
    row,
)
from verdict.orchestration.contracts import EligibilityStage, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
DENIED = "serve_denied:opaque_route_disallowed"


def _ladder(tmp_path: Path) -> EligibilityLadder:
    rows = [row("kiro/a"), row("kiro/b"), row("zz/a")]
    ladder, _ = make_ladder(tmp_path, rows, [conn("kiro"), conn("zz")])
    return ladder


def test_excluded_route_is_skipped_and_reports_the_reason(tmp_path: Path) -> None:
    ladder = _ladder(tmp_path)
    first, _ = ladder.select(TaskRequirements(), now=NOW)
    assert first is not None and first.route_id == "kiro/a"

    ladder.exclude_for_run("kiro/a", DENIED)

    choice, verdicts = ladder.select(TaskRequirements(), now=NOW)
    assert choice is not None and choice.route_id != "kiro/a"
    excluded = by_route(verdicts)["kiro/a"]
    assert excluded.failed_stage is EligibilityStage.TASK_ELIGIBLE
    assert excluded.reason == f"admission:{DENIED}"


def test_exclusion_lasts_for_the_run(tmp_path: Path) -> None:
    ladder = _ladder(tmp_path)
    ladder.exclude_for_run("kiro/a", DENIED)

    _, verdicts = ladder.select(TaskRequirements(), now=NOW + timedelta(days=2))

    assert by_route(verdicts)["kiro/a"].reason == f"admission:{DENIED}"


def test_prefix_cooldown_cools_the_prefix_only_and_expires(tmp_path: Path) -> None:
    ladder = _ladder(tmp_path)
    ladder.cool_for_run("prefix:kiro", 300, now=NOW)

    choice, verdicts = ladder.select(TaskRequirements(), now=NOW)
    seen = by_route(verdicts)
    assert choice is not None and choice.route_id == "zz/a"
    for route in ("kiro/a", "kiro/b"):
        assert seen[route].failed_stage is EligibilityStage.AVAILABLE
        assert seen[route].reason == "cooldown:serve_denied"
        assert seen[route].cooldown_scope == "prefix:kiro"
        assert seen[route].cooldown_until

    later, _ = ladder.select(TaskRequirements(), now=NOW + timedelta(seconds=301))
    assert later is not None and later.route_id.startswith("kiro/")


def test_route_cooldown_leaves_the_sibling_route_alone(tmp_path: Path) -> None:
    ladder = _ladder(tmp_path)
    ladder.cool_for_run("route:kiro/a", 60, now=NOW)

    choice, verdicts = ladder.select(TaskRequirements(), now=NOW)

    assert by_route(verdicts)["kiro/a"].reason == "cooldown:serve_denied"
    assert choice is not None and choice.route_id == "kiro/b"


def test_a_twin_prefix_is_neither_cooled_nor_excluded(tmp_path: Path) -> None:
    ladder = _ladder(tmp_path)
    ladder.exclude_for_run("kiro/a", DENIED)
    ladder.cool_for_run("prefix:kiro", 300, now=NOW)

    choice, verdicts = ladder.select(TaskRequirements(), now=NOW)

    assert choice is not None and choice.route_id == "zz/a"
    assert by_route(verdicts)["zz/a"].failed_stage is None


def test_nothing_is_written_to_the_health_state_file(tmp_path: Path) -> None:
    ladder = _ladder(tmp_path)
    ladder.select(TaskRequirements(), now=NOW)  # a select may persist probe results
    state = tmp_path / "state.json"
    before = state.read_bytes() if state.exists() else b""

    ladder.exclude_for_run("kiro/a", DENIED)
    ladder.cool_for_run("prefix:kiro", 300, now=NOW)
    ladder.cool_for_run("route:zz/a", 60, now=NOW)

    after = state.read_bytes() if state.exists() else b""
    assert after == before
    assert DENIED.encode() not in after and b"serve_denied" not in after
