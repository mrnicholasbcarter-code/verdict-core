"""Tests for the codiv failure classification and provider identity design receipt provider identity tracking."""

import unittest

from verdict.gateway_adapter_runtime import AdapterRouteIdentity, RouteIdentityAttestation


class TestRouteIdentityAttestation(unittest.TestCase):
    """Test identity_summary method for receipt provider/model tracking."""

    def test_identity_summary_no_mismatch(self):
        """When intended and executed match, provider_mismatch is False."""
        resolved = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="codiv/model",
            provider="codiv",
            model_id="diffusiongemma-26b",
            protocol="openai.chat",
        )
        actual = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="codiv/model",
            provider="codiv",
            model_id="diffusiongemma-26b",
            protocol="openai.chat",
        )
        attestation = RouteIdentityAttestation(
            request_id="req-123",
            requested_alias="codiv/model",
            resolved_route=resolved,
            actual_route=actual,
            source="test",
        )

        summary = attestation.identity_summary()

        self.assertEqual(summary["intended_provider"], "codiv")
        self.assertEqual(summary["intended_model"], "diffusiongemma-26b")
        self.assertEqual(summary["executed_provider"], "codiv")
        self.assertEqual(summary["executed_model"], "diffusiongemma-26b")
        self.assertFalse(summary["provider_mismatch"])

    def test_identity_summary_provider_mismatch(self):
        """When providers differ (fallback), provider_mismatch is True."""
        resolved = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="codiv/model",
            provider="codiv",
            model_id="diffusiongemma-26b",
            protocol="openai.chat",
        )
        actual = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="openai/gpt-4",
            provider="openai",
            model_id="gpt-4",
            protocol="openai.chat",
        )
        attestation = RouteIdentityAttestation(
            request_id="req-123",
            requested_alias="codiv/model",
            resolved_route=resolved,
            actual_route=actual,
            source="test",
        )

        summary = attestation.identity_summary()

        self.assertEqual(summary["intended_provider"], "codiv")
        self.assertEqual(summary["intended_model"], "diffusiongemma-26b")
        self.assertEqual(summary["executed_provider"], "openai")
        self.assertEqual(summary["executed_model"], "gpt-4")
        self.assertTrue(summary["provider_mismatch"])

    def test_identity_summary_model_mismatch(self):
        """When models differ, provider_mismatch is True."""
        resolved = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="codiv/model-a",
            provider="codiv",
            model_id="model-a",
            protocol="openai.chat",
        )
        actual = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="codiv/model-b",
            provider="codiv",
            model_id="model-b",
            protocol="openai.chat",
        )
        attestation = RouteIdentityAttestation(
            request_id="req-123",
            requested_alias="codiv/model-a",
            resolved_route=resolved,
            actual_route=actual,
            source="test",
        )

        summary = attestation.identity_summary()

        self.assertEqual(summary["intended_provider"], "codiv")
        self.assertEqual(summary["intended_model"], "model-a")
        self.assertEqual(summary["executed_provider"], "codiv")
        self.assertEqual(summary["executed_model"], "model-b")
        self.assertTrue(summary["provider_mismatch"])

    def test_identity_summary_unattested(self):
        """When actual_route is None, executed fields are None and mismatch is 'unattested'."""
        resolved = AdapterRouteIdentity(
            gateway_id="omniroute",
            route_id="codiv/model",
            provider="codiv",
            model_id="diffusiongemma-26b",
            protocol="openai.chat",
        )
        attestation = RouteIdentityAttestation(
            request_id="req-123",
            requested_alias="codiv/model",
            resolved_route=resolved,
            actual_route=None,  # Unattested
            source="test",
        )

        summary = attestation.identity_summary()

        self.assertEqual(summary["intended_provider"], "codiv")
        self.assertEqual(summary["intended_model"], "diffusiongemma-26b")
        self.assertIsNone(summary["executed_provider"])
        self.assertIsNone(summary["executed_model"])
        self.assertEqual(summary["provider_mismatch"], "unattested")


if __name__ == "__main__":
    unittest.main()
