"""BOD-203 SHADOW collector: pairs OpenJev signals with labeled outcomes offline."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from openjev_shadow_collect import collect

from verdict.decision_signals.calibration import CalibrationRecord
from verdict.decision_signals.contracts import DecisionQuestionV1, DecisionSignalSetV1


class _Provider:
    def __init__(self, signals: dict[str, float] | None, *, fail: bool = False) -> None:
        self._signals, self._fail = signals, fail
        self.questions: list[DecisionQuestionV1] = []

    def signals(self, question: DecisionQuestionV1, *, now: datetime) -> Any:
        self.questions.append(question)
        if self._fail:
            raise TimeoutError("provider down")
        return DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="openjev",
            model="openjev-0.1",
            version="1",
            request_id="r1",
            purpose=question.purpose,
            signals=self._signals,
            confidence=0.9,
            latency_ms=120,
            usage={"input_tokens": 10, "output_tokens": 0},
            input_digest="a" * 64,
            observed_at=now.isoformat(),
            failure_class=None,
            mode="SHADOW",
        )


TASK = {
    "task_id": "pr-640",
    "task_class": "bounded_implementation",
    "summary": "skip too-small windows after context overflow",
    "frontier_needed": False,
    "security_relevant": False,
    "planner_was_frontier": True,
    "verified": True,
    "first_pass": True,
    "label_source": "controller",
}


def test_collect_pairs_signal_with_outcome_and_loads_as_record() -> None:
    provider = _Provider({"frontier_worthy": 0.3, "security_sensitive": 0.1})
    rows = collect([TASK], provider)
    assert provider.questions[0].task_summary == TASK["summary"]
    row = rows[0]
    assert (row["frontier_worthy"], row["security_sensitive"], row["confidence"]) == (0.3, 0.1, 0.9)
    assert row["label_source"] == "controller"
    assert CalibrationRecord.from_dict(row).task_id == "pr-640"


def test_provider_failure_is_no_signal_never_a_cheap_prediction() -> None:
    rows = collect([TASK], _Provider(None, fail=True))
    assert rows[0]["frontier_worthy"] is None
    assert rows[0]["confidence"] is None
    assert rows[0]["signal_failure"] == "TimeoutError"
