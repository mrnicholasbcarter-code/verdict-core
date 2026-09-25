"""Tests for BOD-198 eligibility gates and absence (C & D)."""

import importlib
import unittest

from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.eligibility import EligibilityGate, EligibilityVerdict
from verdict.models import ModelInfo


class TestCodiEligibilityGates(unittest.TestCase):
    """Test C: Codiv through real EligibilityGate - same treatment as any route."""

    def test_codiv_fails_closed_without_availability_evidence(self):
        """C1: Codiv candidate with no availability evidence is not admitted for protected work."""
        gate = EligibilityGate(
            availability_source=None,  # No live availability
            protected_fail_closed=True,
        )

        codiv_model = ModelInfo(
            id="diffusiongemma-26b", provider="codiv", capabilities=["chat"], is_available=True
        )

        result = gate.evaluate(
            candidates=[codiv_model],
            protected=True,  # Protected work
            dev_mode=False,
        )

        # Should NOT be admitted for protected work without availability evidence
        self.assertEqual(len(result.admitted), 0)

        # Check the verdict is RUNTIME_TRUTH_ABSENT (same as any route)
        self.assertEqual(len(result.records), 1)
        record = result.records[0]
        self.assertEqual(record.verdict, EligibilityVerdict.RUNTIME_TRUTH_ABSENT)

    def test_codiv_same_treatment_as_other_providers_non_protected(self):
        """C2: Codiv gets same treatment as other providers with identical fields."""
        gate = EligibilityGate(availability_source=None, protected_fail_closed=True)

        # Codiv model
        codiv_model = ModelInfo(
            id="diffusiongemma-26b", provider="codiv", capabilities=["chat"], is_available=True
        )

        # Another provider with identical fields
        other_model = ModelInfo(
            id="other-model", provider="other", capabilities=["chat"], is_available=True
        )

        # Evaluate both with protected=False
        codiv_result = gate.evaluate(candidates=[codiv_model], protected=False, dev_mode=False)

        other_result = gate.evaluate(candidates=[other_model], protected=False, dev_mode=False)

        # Both should have the same verdict (no special Codiv treatment)
        self.assertEqual(len(codiv_result.records), 1)
        self.assertEqual(len(other_result.records), 1)
        self.assertEqual(codiv_result.records[0].verdict, other_result.records[0].verdict)

    def test_no_codiv_special_case_in_eligibility_modules(self):
        """C guard: Eligibility modules do not import or special-case Codiv."""
        modules_to_check = ["verdict.eligibility", "verdict.autodev_routing"]

        for mod_name in modules_to_check:
            try:
                mod = importlib.import_module(mod_name)
                if hasattr(mod, "__file__") and mod.__file__:
                    with open(mod.__file__) as f:
                        source = f.read()
                    # Should NOT import verdict.providers.codiv
                    self.assertNotIn("from verdict.providers.codiv", source)
                    self.assertNotIn("import verdict.providers.codiv", source)
                    # Should NOT have special Codiv handling
                    self.assertNotIn('provider == "codiv"', source)
                    self.assertNotIn("provider == 'codiv'", source)
            except ImportError:
                pass


class TestCodiAbsence(unittest.TestCase):
    """Test D: Codiv unavailable, another route still selectable."""

    def test_other_route_selected_when_codiv_unavailable(self):
        """D: With Codiv UNAVAILABLE and OpenAI ELIGIBLE, gate admits only OpenAI."""

        # Create models
        codiv_model = ModelInfo(
            id="diffusiongemma-26b",
            provider="codiv",
            capabilities=["chat"],
            is_available=False,  # Will be marked unavailable
        )

        openai_model = ModelInfo(
            id="gpt-4", provider="openai", capabilities=["chat"], is_available=True
        )

        # Availability source that marks Codiv unavailable, OpenAI eligible
        def availability_source(gateway_id: str) -> AvailabilityReport:
            codiv_candidate = AvailabilityCandidate(
                model=codiv_model, state=AvailabilityState.UNAVAILABLE, reasons=("key missing",)
            )
            openai_candidate = AvailabilityCandidate(
                model=openai_model, state=AvailabilityState.ELIGIBLE, reasons=("healthy",)
            )
            return AvailabilityReport(
                candidates=(codiv_candidate, openai_candidate),
                eligible=(openai_candidate,),
                source="test",
                freshness_seconds=1.0,
            )

        # Gate with availability evidence
        gate = EligibilityGate(availability_source=availability_source, protected_fail_closed=True)

        result = gate.evaluate(
            candidates=[codiv_model, openai_model], protected=True, dev_mode=False
        )

        # Only OpenAI should be admitted
        self.assertEqual(len(result.admitted), 1)
        self.assertEqual(result.admitted[0].provider, "openai")

        # Codiv should be excluded with a reason
        exclusions = result.exclusions
        codiv_exclusions = [r for r in exclusions if r.provider == "codiv"]
        self.assertEqual(len(codiv_exclusions), 1)
        # Gate reason is "protected work: live availability unavailable"
        # (the "key missing" detail is in the availability report, not the gate reason)
        self.assertIn("unavailable", codiv_exclusions[0].reason)


if __name__ == "__main__":
    unittest.main()
