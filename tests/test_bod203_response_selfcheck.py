"""Tests for BOD-203 response self-consistency validation in OpenJev adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from verdict.decision_signals.contracts import DecisionQuestionV1
from verdict.decision_signals.openjev import OpenJevSystemOneProvider, _validate_answers
from verdict.gateway_adapters import NormalizedFailureClass

NOW = datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _question() -> DecisionQuestionV1:
    return DecisionQuestionV1(
        purpose="frontier_planning", task_summary="test task", complexity_hints={}
    )


def _provider(transport: Any, *, api_key: str = "sk-test") -> OpenJevSystemOneProvider:
    return OpenJevSystemOneProvider(
        base_url="https://api.codiv.ai", api_key=api_key, transport=transport
    )


def _valid_answers() -> dict[str, Any]:
    """Minimal valid answer set matching _QUESTIONS."""
    return {
        "complexity": {
            "type": "score",
            "score": 1.0,
            "probabilities": {"0": 0.1, "1": 0.6, "2": 0.2, "3": 0.1},
        },
        "decomposability": {
            "type": "score",
            "score": 1.0,
            "probabilities": {"0": 0.3, "1": 0.5, "2": 0.2},
        },
        "ambiguity": {
            "type": "score",
            "score": 0.5,
            "probabilities": {"0": 0.5, "1": 0.3, "2": 0.2},
        },
        "frontier_worthy": {"type": "noul", "noul": 0.1},
        "security_sensitive": {"type": "noul", "noul": 0.05},
        "verification_strength": {
            "type": "score",
            "score": 1.0,
            "probabilities": {"0": 0.2, "1": 0.5, "2": 0.3},
        },
        "context_need": {
            "type": "score",
            "score": 0.5,
            "probabilities": {"0": 0.4, "1": 0.4, "2": 0.2},
        },
    }


def _response_bytes(answers: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "model": "openjev-0.1",
            "answers": answers,
            "usage": {"input_tokens": 50, "output_tokens": 0},
        }
    ).encode()


def _make_transport(answers: dict[str, Any]):
    """Return a transport callable that serves the given answers as a 200."""

    def transport(url: str, headers: dict[str, str], payload: dict[str, Any]):
        return 200, {"x-typesafe-request-id": "req_test"}, _response_bytes(answers)

    return transport


# ---------------------------------------------------------------------------
# _validate_answers unit tests
# ---------------------------------------------------------------------------


class TestValidateAnswersClean:
    """Valid answers pass validation."""

    def test_valid_answers_pass(self) -> None:
        assert _validate_answers(_valid_answers()) is None

    def test_valid_answers_no_probabilities(self) -> None:
        """Score answers without probabilities are still valid (confidence just lower)."""
        ans = _valid_answers()
        del ans["complexity"]["probabilities"]
        assert _validate_answers(ans) is None

    def test_valid_answers_subset(self) -> None:
        """Subset of known questions is valid (provider may omit some)."""
        ans = {"complexity": _valid_answers()["complexity"]}
        assert _validate_answers(ans) is None


class TestValidateUnknownKeys:
    """Unknown answer keys are rejected."""

    def test_unknown_key_rejected(self) -> None:
        ans = _valid_answers()
        ans["invented_signal"] = {"type": "noul", "noul": 0.5}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "unknown answer keys" in reason
        assert "invented_signal" in reason

    def test_multiple_unknown_keys(self) -> None:
        ans = _valid_answers()
        ans["foo"] = {"type": "noul", "noul": 0.1}
        ans["bar"] = {"type": "noul", "noul": 0.2}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "foo" in reason


class TestValidateTypeMismatch:
    """Answer type must match _QUESTIONS definition."""

    def test_score_where_noul_expected(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {
            "type": "score",
            "score": 0.5,
            "probabilities": {"0": 0.5, "1": 0.5},
        }
        reason = _validate_answers(ans)
        assert reason is not None
        assert "frontier_worthy" in reason
        assert "type" in reason

    def test_noul_where_score_expected(self) -> None:
        ans = _valid_answers()
        ans["complexity"] = {"type": "noul", "noul": 0.3}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "complexity" in reason


class TestValidateProbabilityMass:
    """Probability mass must sum to ~1 (tolerance 1e-3)."""

    def test_mass_too_high(self) -> None:
        ans = _valid_answers()
        ans["complexity"]["probabilities"] = {"0": 0.5, "1": 0.3, "2": 0.2, "3": 0.1}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "probability mass" in reason
        assert "complexity" in reason

    def test_mass_too_low(self) -> None:
        ans = _valid_answers()
        ans["complexity"]["probabilities"] = {"0": 0.3, "1": 0.2, "2": 0.1, "3": 0.1}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "probability mass" in reason

    def test_mass_within_tolerance(self) -> None:
        ans = _valid_answers()
        # 0.1 + 0.6 + 0.2 + 0.1001 = 1.0001, within 1e-3
        ans["complexity"]["probabilities"] = {"0": 0.1, "1": 0.6, "2": 0.2, "3": 0.1001}
        assert _validate_answers(ans) is None

    def test_mass_just_outside_tolerance(self) -> None:
        ans = _valid_answers()
        # 0.1 + 0.6 + 0.2 + 0.102 = 1.002, outside 1e-3
        ans["complexity"]["probabilities"] = {"0": 0.1, "1": 0.6, "2": 0.2, "3": 0.102}
        reason = _validate_answers(ans)
        assert reason is not None


class TestValidateNegativeProbabilities:
    """Negative probabilities are rejected."""

    def test_negative_prob_score(self) -> None:
        ans = _valid_answers()
        ans["decomposability"]["probabilities"] = {"0": -0.1, "1": 0.8, "2": 0.3}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "negative probability" in reason
        assert "decomposability" in reason

    def test_negative_prob_zero_is_fine(self) -> None:
        ans = _valid_answers()
        ans["decomposability"]["probabilities"] = {"0": 0.0, "1": 0.7, "2": 0.3}
        assert _validate_answers(ans) is None


class TestValidateNoulRange:
    """Noul values must be in [0, 1]."""

    def test_noul_negative(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": -0.1}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "outside [0, 1]" in reason

    def test_noul_above_one(self) -> None:
        ans = _valid_answers()
        ans["security_sensitive"] = {"type": "noul", "noul": 1.5}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "outside [0, 1]" in reason

    def test_noul_boundary_zero(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 0.0}
        assert _validate_answers(ans) is None

    def test_noul_boundary_one(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 1.0}
        assert _validate_answers(ans) is None

    def test_noul_missing_field(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul"}
        reason = _validate_answers(ans)
        assert reason is not None
        assert "missing" in reason


class TestValidateContradiction:
    """frontier_worthy > 0.5 with complexity trivial (score == 0) is contradictory."""

    def test_frontier_high_complexity_trivial(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 0.9}
        ans["complexity"] = {
            "type": "score",
            "score": 0.0,
            "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0},
        }
        reason = _validate_answers(ans)
        assert reason is not None
        assert "contradiction" in reason

    def test_frontier_low_complexity_trivial_ok(self) -> None:
        """frontier_worthy <= 0.5 with trivial complexity is fine."""
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 0.3}
        ans["complexity"] = {
            "type": "score",
            "score": 0.0,
            "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0},
        }
        assert _validate_answers(ans) is None

    def test_frontier_high_complexity_not_trivial_ok(self) -> None:
        """frontier_worthy > 0.5 with non-trivial complexity is fine."""
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 0.9}
        ans["complexity"] = {
            "type": "score",
            "score": 2.0,
            "probabilities": {"0": 0.05, "1": 0.15, "2": 0.5, "3": 0.3},
        }
        assert _validate_answers(ans) is None


class TestValidateAnswerNotDict:
    """Non-dict answer values are rejected."""

    def test_answer_is_string(self) -> None:
        ans = _valid_answers()
        ans["complexity"] = "trivial"
        reason = _validate_answers(ans)
        assert reason is not None
        assert "not a dict" in reason

    def test_answer_is_list(self) -> None:
        ans = _valid_answers()
        ans["complexity"] = [1, 2, 3]
        reason = _validate_answers(ans)
        assert reason is not None
        assert "not a dict" in reason


# ---------------------------------------------------------------------------
# Integration: provider.signals() returns INVALID_REQUEST on bad answers
# ---------------------------------------------------------------------------


class TestProviderRejectsIncoherent:
    """Provider.signals() returns failure_class=INVALID_REQUEST for incoherent answers."""

    def test_unknown_key_yields_invalid_request(self) -> None:
        ans = _valid_answers()
        ans["bogus"] = {"type": "noul", "noul": 0.5}
        provider = _provider(_make_transport(ans))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.signals is None

    def test_negative_probability_yields_invalid_request(self) -> None:
        ans = _valid_answers()
        ans["complexity"]["probabilities"] = {"0": -0.2, "1": 0.6, "2": 0.4, "3": 0.2}
        provider = _provider(_make_transport(ans))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.signals is None

    def test_probability_mass_wrong_yields_invalid_request(self) -> None:
        ans = _valid_answers()
        ans["ambiguity"]["probabilities"] = {"0": 0.5, "1": 0.5, "2": 0.5}
        provider = _provider(_make_transport(ans))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.signals is None

    def test_contradiction_yields_invalid_request(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 0.8}
        ans["complexity"] = {
            "type": "score",
            "score": 0.0,
            "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0, "3": 0.0},
        }
        provider = _provider(_make_transport(ans))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.signals is None

    def test_valid_answers_produce_signals(self) -> None:
        """Sanity: valid answers still produce signals (no regression)."""
        provider = _provider(_make_transport(_valid_answers()))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class is None
        assert result.signals is not None
        assert "complexity" in result.signals

    def test_noul_out_of_range_yields_invalid_request(self) -> None:
        ans = _valid_answers()
        ans["frontier_worthy"] = {"type": "noul", "noul": 1.5}
        provider = _provider(_make_transport(ans))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.signals is None

    def test_type_mismatch_yields_invalid_request(self) -> None:
        ans = _valid_answers()
        ans["complexity"] = {"type": "noul", "noul": 0.5}
        provider = _provider(_make_transport(ans))
        result = provider.signals(_question(), now=NOW)
        assert result.failure_class == NormalizedFailureClass.INVALID_REQUEST
        assert result.signals is None

    def test_never_raises(self) -> None:
        """Even with bizarre answer shapes, signals() must not raise."""
        bizarre_answers = {"complexity": None, "frontier_worthy": 42}
        provider = _provider(_make_transport(bizarre_answers))
        # Should not raise - the existing except Exception handler catches parse failures
        result = provider.signals(_question(), now=NOW)
        # Either INVALID_REQUEST from validation or from the generic except
        assert result.failure_class is not None
        assert result.signals is None
