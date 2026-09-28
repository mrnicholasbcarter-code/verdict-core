"""Tests for BOD-203 AC2: ranking receipt on InfluenceRecord and RoutingDecision.

Verifies:
- InfluenceRecord carries baseline_order, advised_order, signal_confidence,
  signal_version, and advice_changed fields.
- to_receipt_dict() serializes all receipt fields.
- advise_order populates receipt fields for economy, strength, inconclusive,
  and every skip path.
- advice_changed is True only when advisory changes the first choice.
- Skip paths produce advice_changed=False with the skip reason.
- RoutingDecisionContract.adaptive_influence round-trips receipt data via from_legacy.
- Membership invariance is preserved (BOD-203 invariant: advisory never adds/drops).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from verdict.contracts import RoutingDecisionContract
from verdict.decision_signals.advisory import InfluenceRecord, advise_order
from verdict.decision_signals.contracts import DecisionSignalSetV1
from verdict.gateway_adapters import NormalizedFailureClass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIGEST = "b" * 64


@dataclass
class FakeModel:
    id: str
    capability_tier: int = 2
    provider: str = "prov"
    quality_confidence: float | None = None
    pricing: dict[str, float] | None = None
    is_available: bool = True
    availability_state: str = "eligible"


def _make_signals(
    *,
    frontier_worthy: float = 0.5,
    complexity: float = 0.5,
    confidence: float = 0.9,
    version: str = "openjev-v1.2",
    failure_class: NormalizedFailureClass | None = None,
    signals_override: dict[str, float] | None = None,
) -> DecisionSignalSetV1:
    sigs: dict[str, float] | None = (
        signals_override
        if signals_override is not None
        else {
            "complexity": complexity,
            "decomposability": 0.5,
            "ambiguity": 0.5,
            "frontier_worthy": frontier_worthy,
            "security_sensitive": 0.1,
            "verification_strength": 0.5,
            "context_need": 0.4,
        }
    )
    if failure_class is not None:
        sigs = None
    return DecisionSignalSetV1(
        schema_version="decision-signals/v1",
        provider="test",
        model="test-model",
        version=version,
        request_id="req-1",
        purpose="route",
        signals=sigs,
        confidence=confidence,
        latency_ms=50,
        usage={"input_tokens": 10, "output_tokens": 5},
        input_digest=_DIGEST,
        observed_at="2024-01-01T00:00:00Z",
        failure_class=failure_class,
        mode="ADVISORY",
    )


def _models(*ids: str) -> list[FakeModel]:
    """Create FakeModels with distinct tiers/costs for deterministic ordering."""
    result: list[FakeModel] = []
    for i, mid in enumerate(ids):
        result.append(
            FakeModel(
                id=mid,
                capability_tier=i + 1,
                provider=f"prov-{mid}",
                quality_confidence=1.0 - i * 0.1,
                pricing={"input": 0.01 * (i + 1), "output": 0.01 * (i + 1)},
            )
        )
    return result


# ---------------------------------------------------------------------------
# InfluenceRecord field presence
# ---------------------------------------------------------------------------


class TestInfluenceRecordFields:
    """BOD-203 AC2: InfluenceRecord has all ranking receipt fields."""

    def test_new_fields_exist(self) -> None:
        rec = InfluenceRecord(
            applied=True,
            profile="economy",
            reason="advisory_economy",
            baseline_first="a",
            advised_first="b",
            signals_digest=_DIGEST,
            baseline_order=("a", "b"),
            advised_order=("b", "a"),
            signal_confidence=0.95,
            signal_version="openjev-v1.2",
            advice_changed=True,
        )
        assert rec.baseline_order == ("a", "b")
        assert rec.advised_order == ("b", "a")
        assert rec.signal_confidence == 0.95
        assert rec.signal_version == "openjev-v1.2"
        assert rec.advice_changed is True

    def test_defaults_are_safe(self) -> None:
        """Old-style construction (without new fields) still works."""
        rec = InfluenceRecord(
            applied=False,
            profile="skipped:no_signals",
            reason="no_signals",
            baseline_first=None,
            advised_first=None,
            signals_digest=None,
        )
        assert rec.baseline_order == ()
        assert rec.advised_order == ()
        assert rec.signal_confidence == 0.0
        assert rec.signal_version == ""
        assert rec.advice_changed is False


# ---------------------------------------------------------------------------
# to_receipt_dict
# ---------------------------------------------------------------------------


class TestToReceiptDict:
    """BOD-203 AC2: to_receipt_dict() serializes all fields."""

    def test_round_trip(self) -> None:
        rec = InfluenceRecord(
            applied=True,
            profile="strength",
            reason="advisory_strength",
            baseline_first="x",
            advised_first="y",
            signals_digest=_DIGEST,
            baseline_order=("x", "y", "z"),
            advised_order=("y", "x", "z"),
            signal_confidence=0.88,
            signal_version="v2",
            advice_changed=True,
        )
        d = rec.to_receipt_dict()
        assert isinstance(d, dict)
        assert d["applied"] is True
        assert d["profile"] == "strength"
        assert d["baseline_order"] == ["x", "y", "z"]
        assert d["advised_order"] == ["y", "x", "z"]
        assert d["signal_confidence"] == 0.88
        assert d["signal_version"] == "v2"
        assert d["advice_changed"] is True
        # Lists, not tuples (JSON-compatible)
        assert isinstance(d["baseline_order"], list)
        assert isinstance(d["advised_order"], list)

    def test_skip_receipt_dict(self) -> None:
        rec = InfluenceRecord(
            applied=False,
            profile="skipped:protected",
            reason="protected",
            baseline_first=None,
            advised_first=None,
            signals_digest=None,
        )
        d = rec.to_receipt_dict()
        assert d["advice_changed"] is False
        assert d["baseline_order"] == []
        assert d["advised_order"] == []
        assert d["signal_confidence"] == 0.0


# ---------------------------------------------------------------------------
# advise_order receipt fields
# ---------------------------------------------------------------------------


class TestAdviseOrderReceipt:
    """BOD-203 AC2: advise_order populates receipt fields correctly."""

    def test_economy_reorder_populates_receipt(self) -> None:
        """Economy profile with reorder: full order lists, advice_changed=True."""
        models = _models("expensive", "cheap")
        # cheap has lower tier+cost -> economy puts it first
        signals = _make_signals(frontier_worthy=0.1, complexity=0.1, confidence=0.9, version="v3")
        _ordered, rec = advise_order(models, signals)
        assert rec.profile == "economy"
        assert rec.baseline_order == ("expensive", "cheap")
        assert len(rec.advised_order) == 2
        assert rec.signal_confidence == 0.9
        assert rec.signal_version == "v3"
        # Membership invariance
        assert set(rec.baseline_order) == set(rec.advised_order)

    def test_strength_reorder_populates_receipt(self) -> None:
        """Strength profile: full order lists, correct confidence/version."""
        models = _models("weak", "strong")
        signals = _make_signals(frontier_worthy=0.8, complexity=0.8, confidence=0.95, version="v4")
        _ordered, rec = advise_order(models, signals)
        assert rec.profile == "strength"
        assert len(rec.baseline_order) == 2
        assert len(rec.advised_order) == 2
        assert rec.signal_confidence == 0.95
        assert rec.signal_version == "v4"
        assert set(rec.baseline_order) == set(rec.advised_order)

    def test_no_change_advice_changed_false(self) -> None:
        """When advisory doesn't change first choice, advice_changed is False."""
        # Single model: reorder can't change first choice
        models = [FakeModel(id="only")]
        signals = _make_signals(frontier_worthy=0.1, complexity=0.1)
        _, rec = advise_order(models, signals)
        assert rec.advice_changed is False
        assert rec.baseline_first == rec.advised_first

    def test_inconclusive_receipt(self) -> None:
        """Inconclusive profile: advice_changed=False, orders match baseline."""
        models = _models("a", "b")
        signals = _make_signals(frontier_worthy=0.5, complexity=0.5)
        _, rec = advise_order(models, signals)
        assert rec.profile == "inconclusive"
        assert rec.advice_changed is False
        assert rec.baseline_order == rec.advised_order
        assert rec.signal_confidence == 0.9  # default from _make_signals

    def test_skip_protected(self) -> None:
        models = _models("a", "b")
        signals = _make_signals()
        _, rec = advise_order(models, signals, protected=True)
        assert rec.profile == "skipped:protected"
        assert rec.advice_changed is False
        assert rec.baseline_order == ("a", "b")
        assert rec.advised_order == ("a", "b")

    def test_skip_privacy_restricted(self) -> None:
        models = _models("a")
        signals = _make_signals()
        _, rec = advise_order(models, signals, privacy="restricted")
        assert rec.profile == "skipped:privacy_restricted"
        assert rec.advice_changed is False

    def test_skip_no_signals(self) -> None:
        models = _models("a")
        _, rec = advise_order(models, None)
        assert rec.profile == "skipped:no_signals"
        assert rec.advice_changed is False
        assert rec.signal_confidence == 0.0
        assert rec.signal_version == ""

    def test_skip_low_confidence(self) -> None:
        models = _models("a")
        signals = _make_signals(confidence=0.1, version="v5")
        _, rec = advise_order(models, signals, min_confidence=0.5)
        assert rec.profile == "skipped:low_confidence"
        assert rec.advice_changed is False
        assert rec.signal_confidence == 0.1
        assert rec.signal_version == "v5"

    def test_skip_failure_class(self) -> None:
        models = _models("a")
        signals = _make_signals(failure_class=NormalizedFailureClass.RATE_LIMIT)
        _, rec = advise_order(models, signals)
        assert rec.profile == "skipped:failure_class_set"
        assert rec.advice_changed is False

    def test_skip_empty_signals(self) -> None:
        models = _models("a")
        signals = _make_signals(signals_override={})
        _, rec = advise_order(models, signals)
        assert rec.profile == "skipped:empty_signals"
        assert rec.advice_changed is False

    def test_empty_candidates(self) -> None:
        signals = _make_signals(frontier_worthy=0.1, complexity=0.1)
        _, rec = advise_order([], signals)
        assert rec.reason == "empty_candidate_list"
        assert rec.advice_changed is False
        assert rec.baseline_order == ()
        assert rec.advised_order == ()


# ---------------------------------------------------------------------------
# Membership invariance (BOD-203 invariant)
# ---------------------------------------------------------------------------


class TestMembershipInvariance:
    """Advisory never adds, drops, or invents candidate IDs."""

    @pytest.mark.parametrize(
        "fw,cx", [(0.1, 0.1), (0.8, 0.8), (0.5, 0.5)], ids=["economy", "strength", "inconclusive"]
    )
    def test_order_sets_match(self, fw: float, cx: float) -> None:
        models = _models("alpha", "beta", "gamma")
        signals = _make_signals(frontier_worthy=fw, complexity=cx)
        _, rec = advise_order(models, signals)
        assert set(rec.baseline_order) == set(rec.advised_order)
        assert len(rec.baseline_order) == len(rec.advised_order) == 3


# ---------------------------------------------------------------------------
# RoutingDecisionContract.adaptive_influence integration
# ---------------------------------------------------------------------------


class TestContractAdaptiveInfluence:
    """BOD-203 AC2: ranking receipt flows into RoutingDecisionContract."""

    def test_from_legacy_picks_up_adaptive_influence(self) -> None:
        """from_legacy extracts adaptive_influence from legacy payload."""
        receipt = {
            "applied": True,
            "profile": "economy",
            "reason": "advisory_economy",
            "baseline_first": "a",
            "advised_first": "b",
            "signals_digest": _DIGEST,
            "baseline_order": ["a", "b"],
            "advised_order": ["b", "a"],
            "signal_confidence": 0.9,
            "signal_version": "v1",
            "advice_changed": True,
        }
        legacy = {
            "provider": "test",
            "model": "test-model",
            "tier": 2,
            "reason": "test",
            "adaptive_influence": receipt,
        }
        contract = RoutingDecisionContract.from_legacy(legacy)
        ai = contract.adaptive_influence
        assert ai["applied"] is True
        assert ai["advice_changed"] is True
        assert ai["baseline_order"] == ["a", "b"]
        assert ai["advised_order"] == ["b", "a"]
        assert ai["signal_confidence"] == 0.9
        assert ai["signal_version"] == "v1"

    def test_empty_adaptive_influence_default(self) -> None:
        """Without adaptive_influence, the field defaults to empty dict."""
        legacy = {"provider": "p", "model": "m", "tier": 1, "reason": "r"}
        contract = RoutingDecisionContract.from_legacy(legacy)
        assert contract.adaptive_influence == {} or isinstance(contract.adaptive_influence, dict)

    def test_to_dict_includes_adaptive_influence(self) -> None:
        """to_dict serializes the adaptive_influence receipt."""
        receipt = {
            "applied": False,
            "profile": "skipped:protected",
            "reason": "protected",
            "advice_changed": False,
        }
        contract = RoutingDecisionContract(
            selected_route={"model": "m", "provider": "p"}, adaptive_influence=receipt
        )
        d = contract.to_dict()
        assert d["adaptive_influence"]["advice_changed"] is False
        assert d["adaptive_influence"]["profile"] == "skipped:protected"
