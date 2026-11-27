"""Run-scoped exclusion and cooldown on the eligibility ladder (GitHub issue #1366).

A ``verdict serve`` denial must not outlive the run: the ladder holds the exclusion
and the cooldown in memory and writes neither to its health state file.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tests.test_orch_eligibility import (  # noqa: F401  (the fixture declares the fake providers)
    _subscription_overlay,
    by_route,
    conn,
    make_ladder,
    row,
)
from verdict.orchestration.contracts import EligibilityStage, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder, HealthResult

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


class _SlowAdmitted:
    """Stands in for the immutable admitted set; ``record_confirmation`` can be held open."""

    def __init__(self, routes: frozenset[str], gate: _Gate | None = None) -> None:
        self.routes = routes
        self.gate = gate

    def __contains__(self, route_id: object) -> bool:
        return route_id in self.routes

    def exclude(self, route_ids: Any, stage: Any, reason: str, *, source: str = "") -> Any:
        return _SlowAdmitted(self.routes - frozenset(route_ids), self.gate)

    def record_confirmation(self, route_id: str, **_: Any) -> Any:
        if self.gate is not None:
            self.gate.started.set()
            assert self.gate.release.wait(5)
        return _SlowAdmitted(self.routes, self.gate)  # derived from the set it started with


class _Gate:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()


def test_a_confirmation_in_flight_cannot_put_an_excluded_route_back(tmp_path: Path) -> None:
    """Selection confirms routes on another thread; both replace the admitted set."""
    gate = _Gate()
    ladder = _ladder(tmp_path)
    ladder._admitted = _SlowAdmitted(frozenset({"kiro/a", "kiro/b", "zz/a"}), gate)  # type: ignore[assignment]

    confirming = threading.Thread(
        target=ladder._record_confirmation,
        args=("kiro/b", HealthResult(healthy=True, category=""), NOW),
    )
    confirming.start()
    assert gate.started.wait(5)
    excluding = threading.Thread(target=ladder.exclude_for_run, args=("kiro/a", DENIED))
    excluding.start()
    excluding.join(0.3)
    waited_for_the_confirmation = excluding.is_alive()
    gate.release.set()
    confirming.join(5)
    excluding.join(5)

    assert waited_for_the_confirmation
    assert "kiro/a" not in ladder.admitted  # type: ignore[operator]
    assert "kiro/b" in ladder.admitted  # type: ignore[operator]
