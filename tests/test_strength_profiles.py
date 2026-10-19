import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.strength_profiles import (
    StrengthFailureClass,
    StrengthObservation,
    StrengthProfileError,
    aggregate_strength,
)

NOW = datetime(2026, 7, 31, 12, tzinfo=timezone.utc)
DIGEST = "sha256:" + ("a" * 64)


def observation(
    *,
    route_key: str = "route-a",
    task_family: str = "coding",
    sample_count: int = 1,
    score: float | None = 0.8,
    confidence: float = 0.9,
    failure_class: StrengthFailureClass = StrengthFailureClass.QUALITY,
    observed_at: datetime = NOW,
) -> StrengthObservation:
    return StrengthObservation(
        route_key=route_key,
        task_family=task_family,
        suite_id="suite-v0",
        suite_version="1",
        rubric_id="rubric-code",
        rubric_version="1",
        sample_count=sample_count,
        observed_at=observed_at,
        score=score,
        confidence=confidence,
        failure_class=failure_class,
        evidence_digest=DIGEST,
    )


def test_strength_observation_round_trip_and_digest_are_canonical() -> None:
    item = observation(sample_count=2)

    restored = StrengthObservation.from_dict(item.to_dict())

    assert restored == item
    assert restored.digest == item.digest
    assert item.digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda payload: payload.update({"unexpected": True}), "unknown field"),
        (lambda payload: payload.update({"score": 1.1}), "score"),
        (lambda payload: payload.update({"evidence_digest": "raw-secret"}), "evidence_digest"),
    ],
)
def test_strength_observation_rejects_invalid_contract(change, message: str) -> None:
    payload = observation().to_dict()
    change(payload)

    with pytest.raises(StrengthProfileError, match=message):
        StrengthObservation.from_dict(payload)


def test_non_quality_failures_cannot_carry_or_create_a_quality_score() -> None:
    with pytest.raises(StrengthProfileError, match="cannot have a score"):
        observation(failure_class=StrengthFailureClass.AUTHENTICATION, score=0.0)

    aggregate = aggregate_strength(
        [
            observation(
                failure_class=StrengthFailureClass.AUTHENTICATION,
                score=None,
                observed_at=NOW + timedelta(seconds=1),
            ),
            observation(
                failure_class=StrengthFailureClass.TIMEOUT,
                score=None,
                observed_at=NOW + timedelta(seconds=2),
            ),
        ]
    )[0]

    assert aggregate.score is None
    assert aggregate.confidence is None
    assert aggregate.quality_sample_count == 0
    assert aggregate.total_sample_count == 2
    assert aggregate.ignored_failures == {"authentication": 1, "timeout": 1}


def test_quality_aggregation_is_weighted_and_deterministic() -> None:
    items = [
        observation(sample_count=2, score=0.5, confidence=0.6),
        observation(sample_count=1, score=1.0, confidence=1.0),
        observation(
            route_key="route-b",
            task_family="review",
            score=0.25,
            observed_at=NOW + timedelta(minutes=1),
        ),
    ]

    aggregates = aggregate_strength(reversed(items))

    assert [(item.route_key, item.task_family) for item in aggregates] == [
        ("route-a", "coding"),
        ("route-b", "review"),
    ]
    assert aggregates[0].quality_sample_count == 3
    assert aggregates[0].total_sample_count == 3
    assert aggregates[0].score == pytest.approx(2 / 3)
    assert aggregates[0].confidence == pytest.approx(2.2 / 3)
    assert aggregates[0].ignored_failures == {}


def test_verified_history_changes_next_task_rank_with_smoothed_policy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BOD-438 S10: verified checker history smooths and demotes task-specific score."""
    from datetime import datetime, timezone

    from verdict.candidate_pool import OutcomeObservation
    from verdict.orchestration.contracts import WorkGraph, WorkNode
    from verdict.orchestration.receipt import EventLog, write_run_receipt
    from verdict.outcome_shadow import load_checked_attempts
    from verdict.strength_profiles import score_task_history

    store = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(store))
    fixed = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)

    def _run(run_id: str, *, node_pass: bool) -> Path:
        """One real run bundle: node ``a`` fails on quota (operational, uncounted),
        then both nodes get a terminal success and an independent checker verdict."""
        run_dir = tmp_path / "runs" / run_id
        run_dir.mkdir(parents=True)
        graph = WorkGraph(
            goal="implement the billing export",
            nodes=(
                WorkNode(
                    "a", "route A work", owned_files=("a.py",), verification_command=("pytest",)
                ),
            ),
        )
        (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()), encoding="utf-8")
        log = EventLog(run_dir / "events.jsonl", clock=lambda: fixed)
        log.emit("run_started", run_id=run_id, goal=graph.goal)
        log.emit("dispatch", node_id="a", attempt=1, route_id="cc/claude-sonnet-5")
        log.emit("terminal", node_id="a", attempt=1, ok=False, duration_seconds=1.0)
        log.emit("failure", node_id="a", attempt=1, category="rate_limited")
        log.emit("dispatch", node_id="a", attempt=2, route_id="route/under-test")
        log.emit(
            "terminal",
            node_id="a",
            attempt=2,
            ok=True,
            duration_seconds=3.0,
            reported_model="route/under-test",
        )
        log.emit(
            "verify", node_id="a", ok=node_pass, command="pytest", exit_code=0 if node_pass else 1
        )
        log.emit(
            "node_state", node_id="a", attempt=2, state="VALIDATED" if node_pass else "REJECTED"
        )
        log.emit("run_finished", outcome="COMPLETE" if node_pass else "BLOCKED", reason="")
        write_run_receipt(run_dir)
        return run_dir

    fp_task_family = None
    observations: list[OutcomeObservation] = []
    # Route A: three independently verified runs, all FAILED checks (credit 0).
    # Plus one operational (quota) attempt — never checker-scored, must not count.
    for i in range(3):
        run_dir = _run(f"run-a-{i}", node_pass=False)
        receipt = json.loads((run_dir / "receipt.json").read_text())
        if fp_task_family is None:
            from verdict.candidate_pool import fingerprint_task

            fp_task_family = fingerprint_task(receipt["goal"]).task_family
        joined, rejected = load_checked_attempts(run_dir, store_dir=store, as_of=fixed)
        assert joined, f"expected a checker row for {run_dir}: rejected={rejected}"
        for row in joined:
            observations.append(
                OutcomeObservation(
                    route_id="route/under-test",
                    task_family=row.task_family or fp_task_family,
                    success=row.quality_credit == 1.0,
                    observed_at=row.checked_at,
                    quality_credit=row.quality_credit,
                )
            )

    # Route B: three independently verified runs, all PASSED checks (credit 1).
    for i in range(3):
        run_dir = _run(f"run-b-{i}", node_pass=True)
        joined, rejected = load_checked_attempts(run_dir, store_dir=store, as_of=fixed)
        assert joined, f"expected a checker row for {run_dir}: rejected={rejected}"
        for row in joined:
            observations.append(
                OutcomeObservation(
                    route_id="route/strong",
                    task_family=row.task_family or fp_task_family,
                    success=row.quality_credit == 1.0,
                    observed_at=row.checked_at,
                    quality_credit=row.quality_credit,
                )
            )

    assert fp_task_family is not None

    score_a = score_task_history(
        observations, route_id="route/under-test", task_family=fp_task_family, as_of=fixed
    )
    score_b = score_task_history(
        observations, route_id="route/strong", task_family=fp_task_family, as_of=fixed
    )

    assert score_a.observed_n == 3
    assert score_b.observed_n == 3
    assert score_a.score < score_b.score
    assert score_a.score < 0.4
    assert score_a.demoted is True
    assert score_b.demoted is False

    # Price-only changes never affect the task-history score (A25 invariance):
    # score_task_history has no price input at all, so recomputation is identical.
    score_a_again = score_task_history(
        observations, route_id="route/under-test", task_family=fp_task_family, as_of=fixed
    )
    assert score_a_again == score_a


def test_duplicate_observation_id_counts_once_and_is_not_demoted() -> None:
    """A restarted loader or a root read twice must not double-count one verified attempt."""
    from verdict.candidate_pool import OutcomeObservation
    from verdict.strength_profiles import score_task_history

    as_of = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)
    observed_at = as_of - timedelta(days=1)
    # The same failing checker row, loaded three times (replay/restart duplicate).
    duplicated = [
        OutcomeObservation(
            route_id="route/flaky",
            task_family="implement",
            success=False,
            observed_at=observed_at,
            quality_credit=0.0,
            observation_id="run-x:node-a:1",
        )
        for _ in range(3)
    ]

    result = score_task_history(
        duplicated, route_id="route/flaky", task_family="implement", as_of=as_of
    )

    assert result.observed_n == 1
    # n < 3 (one real failure): never demoted regardless of score.
    assert result.demoted is False


def test_window_excludes_30d_boundary_and_includes_just_inside() -> None:
    """Age must be in the half-open window [0, 30d): exactly 30d is excluded."""
    from verdict.candidate_pool import OutcomeObservation
    from verdict.strength_profiles import score_task_history

    as_of = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)

    at_boundary = OutcomeObservation(
        route_id="route/edge",
        task_family="implement",
        success=True,
        observed_at=as_of - timedelta(days=30),
        quality_credit=1.0,
        observation_id="run-edge:node-a:1",
    )
    result_at_boundary = score_task_history(
        [at_boundary], route_id="route/edge", task_family="implement", as_of=as_of
    )
    assert result_at_boundary.observed_n == 0
    assert result_at_boundary.score == 0.5

    just_inside = OutcomeObservation(
        route_id="route/edge",
        task_family="implement",
        success=True,
        observed_at=as_of - timedelta(days=30) + timedelta(seconds=1),
        quality_credit=1.0,
        observation_id="run-edge:node-a:2",
    )
    result_inside = score_task_history(
        [just_inside], route_id="route/edge", task_family="implement", as_of=as_of
    )
    assert result_inside.observed_n == 1
    age_days = 30.0 - 1.0 / 86400.0
    assert result_inside.weighted_n == pytest.approx(2.0 ** (-age_days / 7.0), rel=1e-9)


def test_seven_day_half_life_weight_is_exact() -> None:
    """At exactly one half-life (7d), the weight is exactly 0.5."""
    from verdict.candidate_pool import OutcomeObservation
    from verdict.strength_profiles import score_task_history

    as_of = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)
    observation = OutcomeObservation(
        route_id="route/decay",
        task_family="implement",
        success=True,
        observed_at=as_of - timedelta(days=7),
        quality_credit=1.0,
        observation_id="run-decay:node-a:1",
    )

    result = score_task_history(
        [observation], route_id="route/decay", task_family="implement", as_of=as_of
    )

    assert result.observed_n == 1
    assert result.weighted_n == pytest.approx(0.5, rel=1e-9)
    # score = (0.5 + 1) / (0.5 + 2) = 0.6
    assert result.score == pytest.approx(0.6, rel=1e-9)
