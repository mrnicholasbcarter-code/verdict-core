"""A successful in-flight task cannot erase newer credential exhaustion."""

from datetime import timedelta
from pathlib import Path

from tests.test_orch_eligibility import NOW, conn, make_ladder, row
from verdict.orchestration.contracts import FailureClassification
from verdict.orchestration.credential_pools import pool_of


def test_concurrent_success_keeps_live_pool_and_route_capacity_blockers(tmp_path: Path) -> None:
    route = "cc/claude-sonnet-5"
    sibling = "cc/claude-haiku-5"
    ladder, _ = make_ladder(
        tmp_path, [row(route, owned_by="claude"), row(sibling, owned_by="claude")], [conn("claude")]
    )
    ladder.record_failure(
        route, FailureClassification("quota_exhausted", "REROUTE", 300, "provider"), now=NOW
    )
    ladder.record_success(route, now=NOW + timedelta(seconds=1))
    assert (
        ladder.dispatch_blocker(route, now=NOW + timedelta(seconds=2)) == f"pool:{pool_of(route)}"
    )
    assert (
        ladder.dispatch_blocker(sibling, now=NOW + timedelta(seconds=2)) == f"pool:{pool_of(route)}"
    )
    assert ladder.dispatch_blocker(sibling, now=NOW + timedelta(seconds=301)) is None


def test_success_clears_old_expired_capacity_evidence(tmp_path: Path) -> None:
    route = "cc/claude-sonnet-5"
    ladder, _ = make_ladder(tmp_path, [row(route, owned_by="claude")], [conn("claude")])
    ladder.record_failure(
        route, FailureClassification("rate_limited", "REROUTE", 1, "provider"), now=NOW
    )
    ladder.record_success(route, now=NOW + timedelta(seconds=2))
    assert ladder.dispatch_blocker(route, now=NOW + timedelta(seconds=2)) is None
