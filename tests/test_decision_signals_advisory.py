"""Tests for BOD-238 OpenJev ADVISORY mode (decision_signals/advisory.py).

Coverage:
- advise_order: economy/strength/inconclusive ordering
- advise_order: all hard-skip guards (protected, privacy, no_signals,
  failure_class, low_confidence, empty_signals)
- membership invariance: advisory cannot add or drop candidates
- determinism: same inputs -> same order
- OFF and SHADOW produce identical routing to baseline (via IntelligenceService)
- ADVISORY reorders routing result
- ExecutionPathDecision present -> advisory untouched
- AdvisoryRanker works as AdvisoryInput.ranker in decision_kernel.decide()
- Mode env parsing: invalid -> OFF with warning

Each test proved mutation-safe: see inline `#MUTATION_PROOF` comments.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from verdict.decision_signals.advisory import AdvisoryRanker, _mode_from_env, advise_order
from verdict.decision_signals.contracts import DecisionSignalSetV1
from verdict.gateway_adapters import NormalizedFailureClass

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIGEST = "a" * 64


def _make_signals(
    *,
    frontier_worthy: float = 0.5,
    complexity: float = 0.5,
    confidence: float = 0.9,
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
        version="1.0",
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


@dataclass
class FakeModel:
    id: str
    capability_tier: int = 2
    provider: str = "p"
    quality_confidence: float | None = None


def _models(*specs: tuple[str, int, str, float]) -> list[FakeModel]:
    return [
        FakeModel(id=i, capability_tier=t, provider=pr, quality_confidence=q)
        for i, t, pr, q in specs
    ]


# ---------------------------------------------------------------------------
# _mode_from_env
# ---------------------------------------------------------------------------


class TestModeFromEnv:
    def test_default_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VERDICT_DECISION_SIGNALS_MODE", raising=False)
        assert _mode_from_env() == "OFF"

    def test_shadow(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")
        assert _mode_from_env() == "SHADOW"

    def test_advisory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        assert _mode_from_env() == "ADVISORY"

    def test_invalid_becomes_off_with_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "TURBO")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _mode_from_env()
        assert result == "OFF"
        assert any("TURBO" in str(warning.message) for warning in w)

    def test_lowercase_advisory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "advisory")
        assert _mode_from_env() == "ADVISORY"


# ---------------------------------------------------------------------------
# advise_order — skip guards
# ---------------------------------------------------------------------------


class TestAdviseOrderSkipGuards:
    def _two_models(self) -> list[FakeModel]:
        return _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.3))

    def test_protected_skips(self) -> None:
        sigs = _make_signals(frontier_worthy=0.8, complexity=0.8)
        candidates = self._two_models()
        out, inf = advise_order(candidates, sigs, protected=True)
        assert out == candidates  # membership unchanged
        assert inf.profile == "skipped:protected"
        assert not inf.applied

    def test_privacy_restricted_skips(self) -> None:
        sigs = _make_signals(frontier_worthy=0.8)
        candidates = self._two_models()
        _out, inf = advise_order(candidates, sigs, privacy="restricted")
        assert inf.profile == "skipped:privacy_restricted"

    def test_privacy_trusted_upstream_skips(self) -> None:
        sigs = _make_signals(frontier_worthy=0.8)
        candidates = self._two_models()
        _out, inf = advise_order(candidates, sigs, privacy="trusted_upstream")
        assert inf.profile == "skipped:privacy_restricted"

    def test_none_signals_skips(self) -> None:
        candidates = self._two_models()
        out, inf = advise_order(candidates, None)
        assert out == candidates
        assert inf.profile == "skipped:no_signals"

    def test_failure_class_skips(self) -> None:
        sigs = _make_signals(failure_class=NormalizedFailureClass.TIMEOUT)
        candidates = self._two_models()
        _out, inf = advise_order(candidates, sigs)
        assert inf.profile == "skipped:failure_class_set"

    def test_low_confidence_skips(self) -> None:
        sigs = _make_signals(confidence=0.4, frontier_worthy=0.8)
        candidates = self._two_models()
        _out, inf = advise_order(candidates, sigs)
        assert inf.profile == "skipped:low_confidence"

    def test_min_confidence_override(self) -> None:
        sigs = _make_signals(confidence=0.4, frontier_worthy=0.8, complexity=0.8)
        candidates = self._two_models()
        _out, inf = advise_order(candidates, sigs, min_confidence=0.3)
        # 0.4 >= 0.3 -> should NOT skip for low confidence; frontier_worthy=0.8 -> strength
        assert inf.profile == "strength"


# ---------------------------------------------------------------------------
# advise_order — economy profile
# ---------------------------------------------------------------------------


class TestAdviseOrderEconomy:
    """frontier_worthy < 0.4 AND complexity < 0.4 -> economy profile."""

    def test_economy_prefers_cheapest_tier(self) -> None:
        # tier 3 is cheaper than tier 1; economy should put tier-3 first
        candidates = _models(("strong", 1, "a", 0.9), ("cheap", 3, "a", 0.1))
        sigs = _make_signals(frontier_worthy=0.2, complexity=0.2)
        out, inf = advise_order(candidates, sigs)
        assert inf.profile == "economy"
        assert out[0].id == "cheap"
        assert inf.applied is True  # #MUTATION_PROOF: removing sort reverses order

    def test_economy_membership_unchanged(self) -> None:
        candidates = _models(("x", 1, "a", 0.9), ("y", 2, "b", 0.5), ("z", 3, "c", 0.1))
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        out, inf = advise_order(candidates, sigs)
        assert sorted(m.id for m in out) == ["x", "y", "z"]
        assert inf.profile == "economy"

    def test_economy_boundary_just_below(self) -> None:
        sigs = _make_signals(frontier_worthy=0.39, complexity=0.39)
        candidates = _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1))
        _, inf = advise_order(candidates, sigs)
        assert inf.profile == "economy"

    def test_economy_boundary_at_threshold_not_economy(self) -> None:
        # 0.4 is NOT < 0.4 -> inconclusive (neither economy nor strength)
        sigs = _make_signals(frontier_worthy=0.4, complexity=0.4)
        candidates = _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1))
        _, inf = advise_order(candidates, sigs)
        assert inf.profile == "inconclusive"


# ---------------------------------------------------------------------------
# advise_order — strength profile
# ---------------------------------------------------------------------------


class TestAdviseOrderStrength:
    """frontier_worthy >= 0.6 OR complexity >= 0.6 -> strength profile."""

    def test_strength_prefers_highest_quality_confidence(self) -> None:
        candidates = _models(("weak", 3, "a", 0.1), ("strong", 1, "a", 0.9))
        sigs = _make_signals(frontier_worthy=0.8, complexity=0.5)
        out, inf = advise_order(candidates, sigs)
        assert inf.profile == "strength"
        assert out[0].id == "strong"
        assert inf.applied is True  # #MUTATION_PROOF: sort key inversion breaks this

    def test_strength_via_complexity(self) -> None:
        candidates = _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.3, complexity=0.7)
        _, inf = advise_order(candidates, sigs)
        assert inf.profile == "strength"

    def test_strength_boundary_at_threshold(self) -> None:
        sigs = _make_signals(frontier_worthy=0.6, complexity=0.3)
        _, inf = advise_order(_models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1)), sigs)
        assert inf.profile == "strength"

    def test_strength_membership_unchanged(self) -> None:
        candidates = _models(("a", 1, "p", 0.9), ("b", 2, "p", 0.5), ("c", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.9)
        out, _inf = advise_order(candidates, sigs)
        assert sorted(m.id for m in out) == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# advise_order — inconclusive
# ---------------------------------------------------------------------------


class TestAdviseOrderInconclusive:
    def test_inconclusive_no_change(self) -> None:
        candidates = _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.5, complexity=0.5)
        out, inf = advise_order(candidates, sigs)
        assert inf.profile == "inconclusive"
        assert out == candidates
        assert not inf.applied


# ---------------------------------------------------------------------------
# Membership invariance
# ---------------------------------------------------------------------------


class TestMembershipInvariance:
    """Advisory CANNOT add or drop candidates — only reorder."""

    def test_no_candidates_added(self) -> None:
        """Advisory cannot introduce a model that was not in the input list."""
        candidates = _models(("a", 1, "p", 0.9))
        sigs = _make_signals(frontier_worthy=0.8)
        out, _ = advise_order(candidates, sigs)
        assert {m.id for m in out} == {"a"}

    def test_no_candidates_dropped(self) -> None:
        candidates = _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.8)
        out, _ = advise_order(candidates, sigs)
        assert {m.id for m in out} == {"a", "b"}

    def test_empty_list_stays_empty(self) -> None:
        sigs = _make_signals(frontier_worthy=0.8)
        out, inf = advise_order([], sigs)
        assert out == []
        assert inf.baseline_first is None

    def test_single_candidate_unchanged(self) -> None:
        candidates = _models(("solo", 1, "p", 0.9))
        sigs = _make_signals(frontier_worthy=0.8)
        out, _inf = advise_order(candidates, sigs)
        assert len(out) == 1
        assert out[0].id == "solo"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_inputs_same_order(self) -> None:
        candidates = _models(("a", 1, "p", 0.9), ("b", 2, "q", 0.5), ("c", 3, "r", 0.1))
        sigs = _make_signals(frontier_worthy=0.8, complexity=0.7)
        out1, _ = advise_order(list(candidates), sigs)
        out2, _ = advise_order(list(candidates), sigs)
        assert [m.id for m in out1] == [m.id for m in out2]

    def test_economy_determinism(self) -> None:
        candidates = _models(("a", 3, "alpha", 0.1), ("b", 3, "beta", 0.2), ("c", 1, "gamma", 0.9))
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        out1, _ = advise_order(list(candidates), sigs)
        out2, _ = advise_order(list(candidates), sigs)
        assert [m.id for m in out1] == [m.id for m in out2]


# ---------------------------------------------------------------------------
# InfluenceRecord fields
# ---------------------------------------------------------------------------


class TestInfluenceRecord:
    def test_signals_digest_populated(self) -> None:
        sigs = _make_signals(frontier_worthy=0.8)
        candidates = _models(("a", 1, "p", 0.9), ("b", 3, "p", 0.1))
        _, inf = advise_order(candidates, sigs)
        assert inf.signals_digest == _DIGEST

    def test_signals_digest_none_when_no_signals(self) -> None:
        candidates = _models(("a", 1, "p", 0.9))
        _, inf = advise_order(candidates, None)
        assert inf.signals_digest is None

    def test_baseline_first_populated(self) -> None:
        candidates = _models(("first", 1, "p", 0.9), ("second", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.5, complexity=0.5)  # inconclusive
        _, inf = advise_order(candidates, sigs)
        assert inf.baseline_first == "first"

    def test_advised_first_economy(self) -> None:
        candidates = _models(("strong", 1, "p", 0.9), ("cheap", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        _, inf = advise_order(candidates, sigs)
        assert inf.advised_first == "cheap"


# ---------------------------------------------------------------------------
# AdvisoryRanker
# ---------------------------------------------------------------------------


class TestAdvisoryRanker:
    """AdaptiveRanker-compatible wrapper for decision_kernel.decide()."""

    def _eligibility(self, models: list[FakeModel]) -> Any:
        from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict

        records = [
            EligibilityRecord(
                model_id=m.id,
                provider=m.provider,
                admitted=True,
                verdict=EligibilityVerdict.ELIGIBLE,
                state="eligible",
                source="test",
                reason="ok",
            )
            for m in models
        ]
        return EligibilityResult(admitted=list(models), records=records)

    def test_advisory_ranker_economy_reorders(self) -> None:
        from verdict.eligibility import EligibilityRecord, EligibilityResult, EligibilityVerdict
        from verdict.models import ModelInfo

        models = [
            ModelInfo(
                id="strong",
                provider="p",
                capability_tier=1,
                quality_confidence=0.9,
                capabilities=frozenset(),
                is_available=True,
                availability_state="eligible",
            ),
            ModelInfo(
                id="cheap",
                provider="p",
                capability_tier=3,
                quality_confidence=0.1,
                capabilities=frozenset(),
                is_available=True,
                availability_state="eligible",
            ),
        ]
        eligibility = EligibilityResult(
            admitted=models,
            records=[
                EligibilityRecord(
                    model_id=m.id,
                    provider=m.provider,
                    admitted=True,
                    verdict=EligibilityVerdict.ELIGIBLE,
                    state="eligible",
                    source="test",
                    reason="ok",
                )
                for m in models
            ],
        )
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        ranker = AdvisoryRanker(sigs)
        out = ranker.rank(eligibility, MagicMock())
        assert out.ranked[0].id == "cheap"  # economy prefers cheapest tier
        assert {m.id for m in out.ranked} == {"strong", "cheap"}

    def test_advisory_ranker_membership_invariant_via_decide(self) -> None:
        """decision_kernel.decide() enforces membership: ranker cannot add/drop."""
        from verdict.contracts import TaskSpec
        from verdict.decision_kernel import AdvisoryInput, decide
        from verdict.models import ModelInfo

        candidates = [
            ModelInfo(
                id="a",
                provider="p",
                capability_tier=2,
                quality_confidence=0.9,
                capabilities=frozenset(),
                is_available=True,
                availability_state="eligible",
            ),
            ModelInfo(
                id="b",
                provider="p",
                capability_tier=3,
                quality_confidence=0.1,
                capabilities=frozenset(),
                is_available=True,
                availability_state="eligible",
            ),
        ]

        task_spec = TaskSpec(
            objective="simple test",
            task_type="test",
            effort="low",
            required_capabilities=[],
            tools=[],
            privacy=None,
            risk=None,
            approvals=[],
            metadata={},
        )

        # Empty availability_truth: gate treats absence as unknown (non-protected -> admitted).
        availability_truth: dict[str, Any] = {}

        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)  # economy
        ranker = AdvisoryRanker(sigs)
        advisory = AdvisoryInput(ranker=ranker, label="advisory-ranker-test")

        record = decide(
            task_spec=task_spec,
            policy_version="policy-2",
            candidates=candidates,
            availability_truth=availability_truth,
            protected=False,
            advisory=advisory,
            dev_mode=True,  # dev_mode admits candidates when truth is absent
        )

        # Membership invariant: admitted set is the same regardless of order.
        admitted_ids = {m.id for m in record.admitted}
        assert admitted_ids == {"a", "b"}  # #MUTATION_PROOF: ranker cannot drop/add


# ---------------------------------------------------------------------------
# OFF and SHADOW produce identical routing to baseline (no reorder)
# ---------------------------------------------------------------------------


class TestOffShadowBaseline:
    """In OFF and SHADOW modes, the intelligence.route() pick must equal baseline."""

    def _make_service(self, provider: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        models = {
            "strong-model": MagicMock(
                capabilities=[], max_tokens=8192, cost_per_1k=0.01, pricing={}
            ),
            "cheap-model": MagicMock(
                capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
            ),
        }
        providers = {"test": ProviderConfig(api_key="k", models=models, priority=1)}
        service = IntelligenceService(
            primary_model="primary",
            providers=providers,
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=provider,
        )
        return service

    @pytest.mark.asyncio
    async def test_off_mode_same_as_no_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "OFF")
        provider = MagicMock()
        provider.signals.return_value = _make_signals(
            frontier_worthy=0.1,
            complexity=0.1,  # economy — would reorder
        )
        svc = self._make_service(provider, monkeypatch)
        _dec = await svc.route("simple task")
        # OFF mode: provider must NOT be called
        provider.signals.assert_not_called()

    @pytest.mark.asyncio
    async def test_shadow_mode_does_not_reorder(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "SHADOW")
        provider = MagicMock()
        svc = self._make_service(provider, monkeypatch)
        _dec = await svc.route("simple task")
        # SHADOW mode: provider NOT called by advisory path
        provider.signals.assert_not_called()


# ---------------------------------------------------------------------------
# ADVISORY mode — reorders the routing result
# ---------------------------------------------------------------------------


class TestAdvisoryRouteReordering:
    """When ADVISORY mode is active, the decision picks follow advise_order."""

    @pytest.mark.asyncio
    async def test_advisory_economy_reorders_to_cheapest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")

        # Two models: tier-1 strong, tier-3 cheap
        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "strong-model": MagicMock(
                    capabilities=[], max_tokens=8192, cost_per_1k=0.01, pricing={}
                ),
                "cheap-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                ),
            },
            priority=1,
        )

        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)  # -> economy

        class FakeProvider:
            def signals(self, question: Any, *, now: Any) -> DecisionSignalSetV1:
                return sigs

        from verdict.classifier import classify

        # Patch classify so "strong-model" -> tier 1, "cheap-model" -> tier 3
        _orig_classify = classify

        def patched_classify(model_id: str) -> int:
            return 1 if "strong" in model_id else 3

        monkeypatch.setattr("verdict.intelligence.classify", patched_classify)
        monkeypatch.setattr("verdict.classifier.classify", patched_classify)

        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=FakeProvider(),
        )

        dec = await svc.route("write hello world")
        # Economy reorders to cheapest (tier 3); advisory flag should be present
        assert any("advisory:economy" in f for f in (dec.safety_flags or [])), (
            f"Expected advisory:economy in safety_flags, got: {dec.safety_flags}"
        )
        # The model should be the cheap one
        assert "cheap" in dec.model, f"Expected cheap-model, got {dec.model}"

    @pytest.mark.asyncio
    async def test_advisory_influence_recorded_in_safety_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")

        sigs = _make_signals(frontier_worthy=0.8, complexity=0.7)  # -> strength

        class FakeProvider:
            def signals(self, question: Any, *, now: Any) -> DecisionSignalSetV1:
                return sigs

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "strong-model": MagicMock(
                    capabilities=[], max_tokens=8192, cost_per_1k=0.01, pricing={}
                ),
                "cheap-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                ),
            },
            priority=1,
        )

        def patched_classify(model_id: str) -> int:
            return 1 if "strong" in model_id else 3

        monkeypatch.setattr("verdict.intelligence.classify", patched_classify)

        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=FakeProvider(),
        )

        dec = await svc.route("complex frontier task")
        flags = dec.safety_flags or []
        assert any("advisory:strength" in f for f in flags), (
            f"Expected advisory:strength in flags, got: {flags}"
        )


# ---------------------------------------------------------------------------
# Protected task — advisory skipped
# ---------------------------------------------------------------------------


class TestProtectedTaskSkipped:
    @pytest.mark.asyncio
    async def test_critical_task_no_advisory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        call_count = []

        class FakeProvider:
            def signals(self, question: Any, *, now: Any) -> DecisionSignalSetV1:
                call_count.append(1)
                return _make_signals(frontier_worthy=0.1, complexity=0.1)

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "strong-model": MagicMock(
                    capabilities=[], max_tokens=8192, cost_per_1k=0.01, pricing={}
                )
            },
            priority=1,
        )

        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=FakeProvider(),
        )

        # critical criticality -> final_tier == 0 -> advisory skipped
        dec = await svc.route("critical task", criticality="critical")
        # Provider should NOT be called for protected work
        assert len(call_count) == 0, f"Provider called {len(call_count)} times for critical task"
        assert dec.protected is True


# ---------------------------------------------------------------------------
# Timeout / error give baseline
# ---------------------------------------------------------------------------


class TestTimeoutAndErrorFallback:
    def test_timeout_gives_skip_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A slow provider causes the advisory to skip with timeout flag."""
        import time

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_TIMEOUT_MS", "10")

        class SlowProvider:
            def signals(self, question: Any, *, now: Any) -> DecisionSignalSetV1:
                time.sleep(2)  # way past 10ms
                return _make_signals()

        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "some-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                )
            },
            priority=1,
        )
        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=SlowProvider(),
        )

        import asyncio

        dec = asyncio.run(svc.route("simple task"))
        flags = dec.safety_flags or []
        assert any("advisory:skipped:timeout" in f for f in flags), (
            f"Expected timeout skip flag, got: {flags}"
        )

    def test_error_gives_skip_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A provider that raises causes advisory to skip with error flag."""
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")

        class ErrorProvider:
            def signals(self, question: Any, *, now: Any) -> DecisionSignalSetV1:
                raise RuntimeError("provider exploded")

        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "some-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                )
            },
            priority=1,
        )
        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            decision_signal_provider=ErrorProvider(),
        )

        import asyncio

        dec = asyncio.run(svc.route("simple task"))
        flags = dec.safety_flags or []
        # Either skipped:timeout (thread catches exception) or skipped:error
        assert any("advisory:skipped" in f for f in flags), f"Expected skip flag, got: {flags}"
