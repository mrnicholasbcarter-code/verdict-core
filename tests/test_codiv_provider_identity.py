"""Tests for BOD-198 provider identity (codiv_routes)."""

import unittest

from verdict.gateway_adapter_runtime import AdapterRouteIdentity
from verdict.providers.codiv import codiv_routes


class TestCodiProviderIdentity(unittest.TestCase):
    """Test Codiv provider identification via exact prefix matching."""

    def test_exact_prefix_match_codiv(self):
        """Routes with exact codiv provider are included."""
        routes = [
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="codiv/diffusiongemma-26b",
                provider="codiv",
                model_id="diffusiongemma-26b-a4b-it",
                protocol="openai.chat",
            )
        ]
        result = codiv_routes(routes, provider_prefix="codiv")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].provider, "codiv")

    def test_different_providers_excluded(self):
        """Same model name under different providers are excluded."""
        routes = [
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="nvidia/diffusiongemma-26b",
                provider="nvidia",
                model_id="diffusiongemma-26b-a4b-it",
                protocol="openai.chat",
            ),
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="bm/diffusiongemma-26b",
                provider="bm",
                model_id="diffusiongemma-26b-a4b-it",
                protocol="openai.chat",
            ),
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="bluesminds/diffusiongemma-26b",
                provider="bluesminds",
                model_id="diffusiongemma-26b-a4b-it",
                protocol="openai.chat",
            ),
        ]
        result = codiv_routes(routes, provider_prefix="codiv")

        self.assertEqual(len(result), 0)

    def test_mixed_providers(self):
        """Only codiv routes extracted from mixed provider list."""
        routes = [
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="codiv/diffusiongemma-26b",
                provider="codiv",
                model_id="diffusiongemma-26b-a4b-it",
                protocol="openai.chat",
            ),
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="nvidia/diffusiongemma-26b",
                provider="nvidia",
                model_id="diffusiongemma-26b-a4b-it",
                protocol="openai.chat",
            ),
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="codiv/another-model",
                provider="codiv",
                model_id="another-model",
                protocol="openai.chat",
            ),
        ]
        result = codiv_routes(routes, provider_prefix="codiv")

        self.assertEqual(len(result), 2)
        self.assertTrue(all(r.provider == "codiv" for r in result))

    def test_case_sensitive_prefix(self):
        """Prefix matching is case-sensitive."""
        routes = [
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="Codiv/model",
                provider="Codiv",  # Uppercase C
                model_id="model",
                protocol="openai.chat",
            ),
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="CODIV/model",
                provider="CODIV",  # All caps
                model_id="model",
                protocol="openai.chat",
            ),
        ]
        result = codiv_routes(routes, provider_prefix="codiv")

        self.assertEqual(len(result), 0)

    def test_partial_prefix_no_match(self):
        """Partial prefix does not match (codivx, my-codiv)."""
        routes = [
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="codivx/model",
                provider="codivx",
                model_id="model",
                protocol="openai.chat",
            ),
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="my-codiv/model",
                provider="my-codiv",
                model_id="model",
                protocol="openai.chat",
            ),
        ]
        result = codiv_routes(routes, provider_prefix="codiv")

        self.assertEqual(len(result), 0)

    def test_custom_prefix(self):
        """Custom provider prefix works."""
        routes = [
            AdapterRouteIdentity(
                gateway_id="omniroute",
                route_id="custom/model",
                provider="custom",
                model_id="model",
                protocol="openai.chat",
            )
        ]
        result = codiv_routes(routes, provider_prefix="custom")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].provider, "custom")

    def test_empty_routes(self):
        """Empty routes returns empty tuple."""
        result = codiv_routes([], provider_prefix="codiv")
        self.assertEqual(len(result), 0)
        self.assertIsInstance(result, tuple)


if __name__ == "__main__":
    unittest.main()
