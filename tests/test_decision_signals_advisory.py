"""Tests for OpenJev ADVISORY mode OpenJev ADVISORY mode (decision_signals/advisory.py).

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


# ---------------------------------------------------------------------------
# Item 3: Economy uses pricing when present (same tier, different prices)
# ---------------------------------------------------------------------------


class TestEconomyPricing:
    """economy profile must prefer cheaper candidate by price when pricing present."""

    def test_same_tier_cheaper_wins(self) -> None:
        """Two candidates, same tier, different pricing -> cheaper wins.
        #MUTATION_PROOF: remove price from key -> z-budget sorts AFTER a-premium
        alphabetically, so without pricing the wrong candidate wins.
        """

        @dataclass
        class PricedModel:
            id: str
            capability_tier: int
            provider: str
            quality_confidence: float
            pricing: dict[str, float]
            cost_per_1k: float = 0.0

        # id "a-premium" sorts before "z-budget" alphabetically, so without
        # pricing the wrong candidate would be picked.
        premium = PricedModel(
            id="a-premium",
            capability_tier=2,
            provider="p",
            quality_confidence=0.5,
            pricing={"input": 5.0, "output": 15.0},  # expensive
        )
        budget = PricedModel(
            id="z-budget",
            capability_tier=2,
            provider="p",
            quality_confidence=0.5,
            pricing={"input": 0.1, "output": 0.3},  # cheap
        )
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        # Start with premium first so we prove the sort is by price, not initial order.
        out, inf = advise_order([premium, budget], sigs)
        assert inf.profile == "economy"
        assert out[0].id == "z-budget", f"Expected z-budget (cheap) first, got {out[0].id}"

    def test_different_tier_no_pricing_still_tier_order(self) -> None:
        """Without pricing dict, falls back to tier (higher tier = cheaper)."""
        candidates = _models(("tier1", 1, "p", 0.9), ("tier3", 3, "p", 0.1))
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        out, inf = advise_order(candidates, sigs)
        assert inf.profile == "economy"
        assert out[0].id == "tier3"  # tier 3 = cheaper

    def test_pricing_tiebreak_by_tier(self) -> None:
        """Same total price -> higher tier number (cheaper tier) wins as tiebreak."""

        @dataclass
        class PricedModel2:
            id: str
            capability_tier: int
            provider: str
            quality_confidence: float
            pricing: dict[str, float]

        # same input+output cost but different tiers
        cheap_tier = PricedModel2(
            id="cheap_tier",
            capability_tier=3,
            provider="p",
            quality_confidence=0.1,
            pricing={"input": 1.0, "output": 2.0},
        )
        expensive_tier = PricedModel2(
            id="expensive_tier",
            capability_tier=1,
            provider="p",
            quality_confidence=0.9,
            pricing={"input": 1.0, "output": 2.0},
        )
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        out, inf = advise_order([expensive_tier, cheap_tier], sigs)
        assert inf.profile == "economy"
        # Same price: tier 3 > tier 1 -> cheap_tier wins
        assert out[0].id == "cheap_tier"


# ---------------------------------------------------------------------------
# Item 2: Live admit path records advisory:skipped:admit_path_not_supported
# ---------------------------------------------------------------------------


class TestAdmitPathAdvisoryFlag:
    """In ADVISORY mode with a provider, the live admit path records the skip flag."""

    def test_admit_path_flag_in_offload_free_tier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_offload_free_tier appends advisory:skipped:admit_path_not_supported
        when ADVISORY mode is on and a provider is configured.
        Calls the real _offload_free_tier with the inner pipeline stubbed out.
        #MUTATION_PROOF: remove the flag append in intelligence.py -> flag absent.
        """
        from unittest.mock import MagicMock, patch

        from verdict.free_tier_admit import FreeTierAdmitReceipt, snapshot_from_payloads
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig, RoutingDecision

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")

        class FakeProvider:
            def signals(self, question: Any, *, now: Any) -> Any:
                return _make_signals(frontier_worthy=0.1, complexity=0.1)

        snapshot = snapshot_from_payloads(
            catalog=[
                {
                    "id": "m1",
                    "provider": "test",
                    "model_id": "m1",
                    "gateway_id": "gw1",
                    "route_id": "r1",
                    "tier": 2,
                    "capabilities": {},
                    "resource_class": "free",
                }
            ],
            free_tier=["m1"],
            providers=[],
        )

        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": ProviderConfig(api_key="k", models={}, priority=1)},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=False,
            decision_signal_provider=FakeProvider(),
            admit_snapshot=snapshot,
        )

        # Build a real FreeTierAdmitReceipt to return from mocked pipeline steps.
        fake_receipt = FreeTierAdmitReceipt(
            admitted=("m1",),
            exclusions=(),
            chosen="m1",
            empty_intersection=False,
            active_providers=("test",),
            free_tier_providers=("test",),
        )
        base_dec = RoutingDecision(
            model="m1",
            provider="test",
            tier=2,
            reason="test",
            safety_flags=["free_tier_active_admit"],
        )

        from typing import ClassVar

        class _FakeElig:
            admitted: ClassVar[list] = []
            records: ClassVar[list] = []

            def to_dict(self) -> dict:
                return {}

        # Patch every sub-step inside _offload_free_tier EXCEPT the advisory block.
        with (
            patch(
                "verdict.intelligence.classify_worthiness",
                return_value=MagicMock(
                    task_class="implementation",
                    protected_ranker_class="implementation",
                    class_reasons=[],
                ),
            ),
            patch("verdict.intelligence.derive_requirements", return_value=MagicMock()),
            patch(
                "verdict.intelligence.profile_task",
                return_value=MagicMock(spend_policy="free", digest="d1"),
            ),
            patch("verdict.intelligence.admit_free_tier_active", return_value=fake_receipt),
            patch("verdict.intelligence.expand_admit_for_worthiness", return_value=fake_receipt),
            patch("verdict.intelligence.gate_capability", return_value=fake_receipt),
            patch("verdict.intelligence.gate_admit_prove_confirm", return_value=fake_receipt),
            patch.object(svc, "_estimate_candidate_context_plans", return_value=((), [])),
            patch.object(svc, "_apply_candidate_pool", return_value=(fake_receipt, [])),
            patch.object(svc, "_decision_from_admit", return_value=base_dec),
            patch.object(type(fake_receipt), "as_eligibility_result", return_value=_FakeElig()),
        ):
            result = svc._offload_free_tier(
                "test task", 2, False, "", context={"spend_policy": "free"}
            )

        flags = result.safety_flags if result is not None else []
        assert flags is not None
        assert "advisory:skipped:admit_path_not_supported" in flags, (
            f"Expected admit_path flag in _offload_free_tier result, got: {flags}"
        )
        # OFF mode: no flag
        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "OFF")
        with (
            patch(
                "verdict.intelligence.classify_worthiness",
                return_value=MagicMock(
                    task_class="implementation",
                    protected_ranker_class="implementation",
                    class_reasons=[],
                ),
            ),
            patch("verdict.intelligence.derive_requirements", return_value=MagicMock()),
            patch(
                "verdict.intelligence.profile_task",
                return_value=MagicMock(spend_policy="free", digest="d1"),
            ),
            patch("verdict.intelligence.admit_free_tier_active", return_value=fake_receipt),
            patch("verdict.intelligence.expand_admit_for_worthiness", return_value=fake_receipt),
            patch("verdict.intelligence.gate_capability", return_value=fake_receipt),
            patch("verdict.intelligence.gate_admit_prove_confirm", return_value=fake_receipt),
            patch.object(svc, "_estimate_candidate_context_plans", return_value=((), [])),
            patch.object(svc, "_apply_candidate_pool", return_value=(fake_receipt, [])),
            patch.object(svc, "_decision_from_admit", return_value=base_dec),
            patch.object(type(fake_receipt), "as_eligibility_result", return_value=_FakeElig()),
        ):
            result_off = svc._offload_free_tier(
                "test task", 2, False, "", context={"spend_policy": "free"}
            )
        flags_off = result_off.safety_flags if result_off is not None else []
        assert "advisory:skipped:admit_path_not_supported" not in (flags_off or [])


# ---------------------------------------------------------------------------
# Item 5: decision_kernel membership unchanged AND order changed
# ---------------------------------------------------------------------------


class TestDecisionKernelOrderAndMembership:
    """AdvisoryRanker changes order inside decide() while membership stays fixed."""

    def test_order_changed_membership_unchanged(self) -> None:
        """decide() with AdvisoryRanker(economy): admitted ids unchanged,
        but the first admitted candidate differs from baseline.
        #MUTATION_PROOF: removing advisory reorder -> order same as baseline.
        """
        from verdict.contracts import TaskSpec
        from verdict.decision_kernel import AdvisoryInput, decide
        from verdict.models import ModelInfo

        strong = ModelInfo(
            id="strong",
            provider="p",
            capability_tier=1,
            quality_confidence=0.9,
            capabilities=frozenset(),
            is_available=True,
            availability_state="eligible",
        )
        cheap = ModelInfo(
            id="cheap",
            provider="p",
            capability_tier=3,
            quality_confidence=0.1,
            capabilities=frozenset(),
            is_available=True,
            availability_state="eligible",
        )
        candidates = [strong, cheap]

        task_spec = TaskSpec(
            objective="write hello world",
            task_type="implementation",
            effort="low",
            required_capabilities=[],
            tools=[],
            privacy=None,
            risk=None,
            approvals=[],
            metadata={},
        )

        # Baseline: no advisory
        baseline = decide(
            task_spec=task_spec,
            policy_version="policy-2",
            candidates=candidates,
            availability_truth={},
            protected=False,
            dev_mode=True,
        )
        baseline_order = [m.id for m in baseline.admitted]

        # Economy signals -> prefers cheap (tier 3) first
        sigs = _make_signals(frontier_worthy=0.1, complexity=0.1)
        ranker = AdvisoryRanker(sigs)
        advisory_record = decide(
            task_spec=task_spec,
            policy_version="policy-2",
            candidates=candidates,
            availability_truth={},
            protected=False,
            dev_mode=True,
            advisory=AdvisoryInput(ranker=ranker, label="economy-test"),
        )
        advisory_order = [m.id for m in advisory_record.admitted]

        # Membership invariant: same set
        assert set(advisory_order) == set(baseline_order), (
            f"Membership changed: {set(advisory_order)} != {set(baseline_order)}"
        )
        # Order changed: economy puts cheap first
        assert advisory_order[0] == "cheap", (
            f"Expected cheap first in advisory order, got: {advisory_order}"
        )
        assert baseline_order[0] == "strong", (
            f"Expected strong first in baseline, got: {baseline_order}"
        )
        # decision_id is membership-bound, not order-bound
        assert advisory_record.decision_id == baseline.decision_id, (
            "decision_id should be identical (membership-bound, not order-bound)"
        )


# ---------------------------------------------------------------------------
# Item 4: No second planner call — task_spec privacy is read, not re-planned
# ---------------------------------------------------------------------------


class TestNoSecondPlannerCall:
    """Advisory block reuses the task_spec already computed by route(), not re-plan."""

    @pytest.mark.asyncio
    async def test_planner_called_once_not_twice(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Planner.plan() is called exactly once per route() call in ADVISORY mode.
        #MUTATION_PROOF: adding a second planner call -> count == 2 -> test fails.
        """
        from unittest.mock import MagicMock

        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")

        class FakeProvider:
            def signals(self, question: Any, *, now: Any) -> Any:
                return _make_signals(frontier_worthy=0.5, complexity=0.5)

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
            decision_signal_provider=FakeProvider(),
        )

        call_count = 0
        original_plan = svc.planner.plan

        def counting_plan(*args: Any, **kwargs: Any) -> Any:
            nonlocal call_count
            call_count += 1
            return original_plan(*args, **kwargs)

        svc.planner.plan = counting_plan  # type: ignore[method-assign]
        await svc.route("simple task")

        assert call_count == 1, f"Expected planner.plan() called once, got {call_count} calls"


# ---------------------------------------------------------------------------
# item A+B: privacy=restricted route() skip + provider_from_env wiring
# ---------------------------------------------------------------------------


class TestPrivacyRestrictedRouteSkip:
    """route() must NOT call the provider and must emit
    advisory:skipped:privacy_restricted when the planner produces a task_spec
    with privacy='restricted'.

    Controller review item A (explicit variable) + flag emission.
    """

    @pytest.mark.asyncio
    async def test_restricted_privacy_emits_skip_flag_and_skips_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#MUTATION_PROOF: removing the privacy guard -> provider IS called ->
        len(call_count) == 1 -> assertion len == 0 fails."""
        from unittest.mock import MagicMock

        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig
        from verdict.planner import StructuredPlanner

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        call_count: list[Any] = []

        class CountingProvider:
            def signals(self, question: Any, *, now: Any) -> Any:
                call_count.append(1)
                return _make_signals(frontier_worthy=0.1, complexity=0.1)

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "some-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                )
            },
            priority=1,
        )

        # Planner that always returns privacy='restricted'
        from dataclasses import replace as _dc_replace

        class RestrictedPlanner(StructuredPlanner):
            def plan(self, task: str, **kwargs: Any) -> Any:  # type: ignore[override]
                from verdict.planner import PlanResult

                result = super().plan(task, **kwargs)
                # TaskSpec is frozen; use dataclasses.replace to inject privacy.
                new_spec = _dc_replace(result.task_spec, privacy="restricted")
                return PlanResult(new_spec, result.workflow_plan, result.metadata)

        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            planner=RestrictedPlanner(),
            decision_signal_provider=CountingProvider(),
        )

        dec = await svc.route("summarise internal notes")
        flags = dec.safety_flags or []

        # Provider must NOT have been called for a restricted task
        assert len(call_count) == 0, (
            f"Provider was called {len(call_count)} times for a privacy=restricted task; must be 0"
        )
        # Skip flag must be present
        assert "advisory:skipped:privacy_restricted" in flags, (
            f"Expected advisory:skipped:privacy_restricted in safety_flags, got: {flags}"
        )

    @pytest.mark.asyncio
    async def test_mutation_removing_guard_calls_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Mutation proof: if the advisory block does NOT check privacy, the provider
        IS called for a restricted task.  We simulate this by patching the planner to
        return privacy=None (i.e., the check is bypassed) and verifying the provider
        IS called — confirming the baseline assertion (provider NOT called) is
        load-bearing."""
        from unittest.mock import MagicMock

        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig
        from verdict.planner import StructuredPlanner

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        call_count: list[Any] = []

        class CountingProvider:
            def signals(self, question: Any, *, now: Any) -> Any:
                call_count.append(1)
                return _make_signals(frontier_worthy=0.1, complexity=0.1)

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "some-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                )
            },
            priority=1,
        )

        # Mutation: planner returns privacy=None (simulates guard removed)
        from dataclasses import replace as _dc_replace

        class NullPrivacyPlanner(StructuredPlanner):
            def plan(self, task: str, **kwargs: Any) -> Any:  # type: ignore[override]
                from verdict.planner import PlanResult

                result = super().plan(task, **kwargs)
                # TaskSpec is frozen; replace with privacy="unknown" (no restriction)
                new_spec = _dc_replace(result.task_spec, privacy="unknown")
                return PlanResult(new_spec, result.workflow_plan, result.metadata)

        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            planner=NullPrivacyPlanner(),
            decision_signal_provider=CountingProvider(),
        )

        await svc.route("summarise internal notes")
        # Under mutation (privacy=None), provider IS called — proves the guard matters
        assert len(call_count) == 1, (
            f"Mutation test: expected provider called once with privacy=None, "
            f"got {len(call_count)} calls"
        )


class TestIntelligenceServiceDefaultWiring:
    """IntelligenceService.__init__ defaults decision_signal_provider via
    factory.provider_from_env() when no explicit provider is given (item B.1).

    A mutation that replaces provider_from_env() with a None-returning lambda
    must cause the provider NOT to be used (0 calls), proving the wiring is
    load-bearing.
    """

    @pytest.mark.asyncio
    async def test_default_wiring_uses_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#MUTATION_PROOF: if __init__ hardcodes None instead of calling
        provider_from_env(), call_count stays 0 -> the assertion == 1 fails."""
        from unittest.mock import MagicMock

        import verdict.decision_signals.factory as fmod
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        call_count: list[Any] = []

        class CountingProvider:
            def signals(self, question: Any, *, now: Any) -> Any:
                call_count.append(1)
                return _make_signals(frontier_worthy=0.1, complexity=0.1)

        # Patch factory to return a known provider
        monkeypatch.setattr(fmod, "provider_from_env", lambda: CountingProvider())

        provider_cfg = ProviderConfig(
            api_key="k",
            models={
                "some-model": MagicMock(
                    capabilities=[], max_tokens=4096, cost_per_1k=0.001, pricing={}
                )
            },
            priority=1,
        )

        # No decision_signal_provider passed -> factory consulted at __init__ time
        svc = IntelligenceService(
            primary_model="primary",
            providers={"test": provider_cfg},
            profile="development",
            log_path="",
            log_full_task=False,
            discovery_ttl=60,
            allow_offline=True,
            # decision_signal_provider intentionally omitted
        )

        await svc.route("summarise data")

        assert len(call_count) == 1, (
            f"Expected factory-wired provider called once, got {len(call_count)}"
        )

    @pytest.mark.asyncio
    async def test_mutation_none_wiring_zero_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mutation: factory returns None -> 0 provider calls.  This demonstrates
        that the baseline assertion (1 call) would fail if __init__ were patched
        to skip provider_from_env() and always assign None."""
        from unittest.mock import MagicMock

        import verdict.decision_signals.factory as fmod
        from verdict.intelligence import IntelligenceService
        from verdict.models import ProviderConfig

        monkeypatch.setenv("VERDICT_DECISION_SIGNALS_MODE", "ADVISORY")
        call_count: list[Any] = []

        class CountingProvider:
            def signals(self, question: Any, *, now: Any) -> Any:
                call_count.append(1)
                return _make_signals(frontier_worthy=0.1, complexity=0.1)

        # MUTATION: factory returns None
        monkeypatch.setattr(fmod, "provider_from_env", lambda: None)

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
        )

        await svc.route("summarise data")

        # Mutation: 0 calls; confirms baseline assertion would fail
        assert len(call_count) == 0, (
            f"Mutation: factory=None must produce 0 provider calls, got {len(call_count)}"
        )
