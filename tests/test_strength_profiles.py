from datetime import datetime, timedelta, timezone

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
