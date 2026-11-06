"""Served-route verification from OmniRoute 3.8.53 response headers."""

from __future__ import annotations

import httpx
import pytest

from verdict.served_identity import (
    OMNIROUTE_PROVIDER_ALIASES,
    check_served,
    served_identity_from_headers,
)

# Header shape from OmniRoute v3.8.53 buildOmniRouteResponseMetaHeaders and
# withSelectedConnectionHeader for a non-combo request to cc/claude-opus-5-5.
SERVED_OPUS = {
    "X-OmniRoute-Provider": "cc",
    "X-OmniRoute-Model": "claude-opus-5-5",
    "X-OmniRoute-Decision": "strategy=single; provider=cc; latency_ms=812",
    "X-OmniRoute-Selected-Connection-Id": "conn-7f3a",
    "X-OmniRoute-Request-Id": "req-1",
    "X-OmniRoute-Version": "3.8.53",
}


def test_exact_served_route_is_verified_and_keeps_connection() -> None:
    check = check_served("cc/claude-opus-5-5", served_identity_from_headers(SERVED_OPUS))
    assert check.status == "verified"
    assert check.served_route == "cc/claude-opus-5-5"
    assert check.served is not None and check.served.connection_id == "conn-7f3a"
    assert check.to_dict()["served"]["decision_strategy"] == "single"


@pytest.mark.parametrize(
    "selected",
    ["omniroute/cc/claude-opus-5-5", "claude/claude-opus-5-5", "no-think/cc/claude-opus-5-5"],
)
def test_gateway_prefix_provider_id_and_catalog_mirrors_route_to_the_same_target(
    selected: str,
) -> None:
    assert check_served(selected, served_identity_from_headers(SERVED_OPUS)).status == "verified"


def test_claude_mirror_of_another_provider_routes_to_that_provider() -> None:
    headers = {**SERVED_OPUS, "X-OmniRoute-Provider": "gc", "X-OmniRoute-Model": "grok-4.6"}
    headers["X-OmniRoute-Decision"] = "strategy=single; provider=gc; latency_ms=5"
    assert check_served("claude/gc/grok-4.6", served_identity_from_headers(headers)).status == (
        "verified"
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"X-OmniRoute-Model": "claude-haiku-5-5"}, "model_differs"),
        (
            {
                "X-OmniRoute-Provider": "agy",
                "X-OmniRoute-Decision": "strategy=single; provider=agy; latency_ms=1",
            },
            "provider_differs",
        ),
        (
            {"X-OmniRoute-Decision": "strategy=single; provider=cx; latency_ms=1"},
            "decision_provider_differs",
        ),
        (
            {"X-OmniRoute-Decision": "strategy=priority; provider=cc; latency_ms=1"},
            "combo_strategy_on_concrete_route",
        ),
    ],
)
def test_a_known_difference_is_a_mismatch(changes: dict[str, str], reason: str) -> None:
    check = check_served(
        "cc/claude-opus-5-5", served_identity_from_headers({**SERVED_OPUS, **changes})
    )
    assert check.status == "mismatch"
    assert reason in check.reasons


def test_no_identity_headers_is_unknown_never_verified() -> None:
    assert served_identity_from_headers({"content-type": "application/json"}) is None
    check = check_served("cc/claude-opus-5-5", None)
    assert check.status == "unknown"
    assert check.served_route is None


@pytest.mark.parametrize("missing", ["X-OmniRoute-Provider", "X-OmniRoute-Model"])
def test_a_missing_provider_or_model_is_unknown(missing: str) -> None:
    headers = {k: v for k, v in SERVED_OPUS.items() if k != missing}
    check = check_served("cc/claude-opus-5-5", served_identity_from_headers(headers))
    assert check.status == "unknown"


def test_repeated_conflicting_header_is_unknown() -> None:
    headers = httpx.Headers([*SERVED_OPUS.items(), ("x-omniroute-model", "claude-haiku-5-5")])
    check = check_served("cc/claude-opus-5-5", served_identity_from_headers(headers))
    assert check.status == "unknown"
    assert "conflicting_x-omniroute-model" in check.reasons


def test_control_character_in_header_is_unknown() -> None:
    headers = {**SERVED_OPUS, "X-OmniRoute-Model": "claude-opus-5-5\x0b"}
    assert check_served("cc/claude-opus-5-5", served_identity_from_headers(headers)).status == (
        "unknown"
    )


@pytest.mark.parametrize("selected", ["auto/best-coding", "combo/fast", "omniroute/auto"])
def test_opaque_selection_records_member_but_proves_nothing(selected: str) -> None:
    check = check_served(selected, served_identity_from_headers(SERVED_OPUS))
    assert check.status == "opaque"
    assert check.served_route == "cc/claude-opus-5-5"


def test_unqualified_selection_is_unknown() -> None:
    assert (
        check_served("gpt-6.1-sol", served_identity_from_headers(SERVED_OPUS)).status == "unknown"
    )


def test_alias_table_matches_omniroute_header_aliases() -> None:
    assert OMNIROUTE_PROVIDER_ALIASES["claude"] == "cc"
    assert OMNIROUTE_PROVIDER_ALIASES["kilocode"] == "kc"
    assert OMNIROUTE_PROVIDER_ALIASES["grok-cli"] == "gc"
    assert len(set(OMNIROUTE_PROVIDER_ALIASES.values())) == len(OMNIROUTE_PROVIDER_ALIASES)


@pytest.mark.parametrize(
    ("selected", "provider", "model"),
    [
        # OmniRoute 3.8.53 reports the requested catalog id in X-OmniRoute-Model
        # (modelAfterFamilyFallback), before it strips an effort suffix upstream.
        ("cc/claude-opus-5-5-high", "cc", "claude-opus-5-5-high"),
        ("cx/gpt-6.1-sol-low", "cx", "gpt-6.1-sol-low"),
        ("kc/anthropic/claude-opus-5.5", "kc", "anthropic/claude-opus-5.5"),
        ("kilocode/anthropic/claude-opus-5.5", "kc", "anthropic/claude-opus-5.5"),
    ],
)
def test_effort_suffix_and_nested_catalog_ids_verify(
    selected: str, provider: str, model: str
) -> None:
    headers = {
        "X-OmniRoute-Provider": provider,
        "X-OmniRoute-Model": model,
        "X-OmniRoute-Decision": f"strategy=single; provider={provider}; latency_ms=3",
    }
    assert check_served(selected, served_identity_from_headers(headers)).status == "verified"


def test_family_fallback_to_another_model_is_a_mismatch() -> None:
    headers = {**SERVED_OPUS, "X-OmniRoute-Model": "claude-opus-5"}
    check = check_served("cc/claude-opus-5-5-high", served_identity_from_headers(headers))
    assert check.status == "mismatch"
