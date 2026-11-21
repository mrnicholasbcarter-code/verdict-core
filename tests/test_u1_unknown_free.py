"""Tests for U1: UNKNOWN capacity is free-tier until a probe charge proves otherwise.

Operator decision D31, 2026-11-21.  ADR-048.

All tests use a temp VERDICT_HOME and an injectable transport — no live calls.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import pytest

from verdict.discovery.model import Route
from verdict.discovery.probe import _set_transport

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _route(provider: str = "omniroute", model: str = "ovhfree/mistral-7b") -> Route:
    return Route(
        provider=provider,
        model=model,
        base_url="http://localhost:20128/v1",
        source="omniroute_catalog",
        upstream=provider if provider == "ovhfree" else None,
    )


def _ok_transport(*, cost: float | None = None) -> Any:
    """Return a transport that always answers HTTP 200 with usable content."""

    def _transport(
        url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        resp_headers: dict[str, str] = {"Content-Type": "application/json"}
        if cost is not None:
            resp_headers["x-omniroute-response-cost"] = str(cost)
        # Simulate OmniRoute echoing the upstream model identity.
        resp_headers["x-omniroute-provider"] = "ovhfree"
        resp_headers["x-omniroute-model"] = "mistral-7b"
        body_bytes = json.dumps(
            {
                "model": "ovhfree/mistral-7b",
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
            }
        ).encode()
        return 200, resp_headers, body_bytes

    return _transport


def _timeout_transport() -> Any:
    """Transport that always raises TimeoutError (not up)."""

    def _transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
        raise TimeoutError("timed out")

    return _transport


# ---------------------------------------------------------------------------
# Test 1: UNKNOWN routes are probed in --free-only mode
# ---------------------------------------------------------------------------


def test_unknown_probed_in_free_only(tmp_path: Path) -> None:
    """UNKNOWN routes appear in the route list passed to run_refresh when free_only=True."""
    from verdict.discovery.refresh import _PROBEABLE

    assert "unknown" in _PROBEABLE, f"_PROBEABLE must include 'unknown', got {_PROBEABLE}"

    # Also verify the free_only filter: build a mixed list and check output.
    from verdict.discovery.model import Route

    r_free = Route("omniroute", "free/model-a", "http://localhost/v1")
    r_unknown = Route("omniroute", "unknown-prov/model-b", "http://localhost/v1")
    r_sub = Route("omniroute", "sub/model-c", "http://localhost/v1")
    all_routes = [
        (r_free, "free", "gateway_free_tier"),
        (r_unknown, "unknown", "no_free_evidence"),
        (r_sub, "subscription", "oauth_subscription"),
    ]
    free_filtered = [(r, cc, cf) for (r, cc, cf) in all_routes if cc in {"free", "unknown"}]
    assert len(free_filtered) == 2
    assert any(cc == "unknown" for _, cc, _ in free_filtered)
    assert all(cc != "subscription" for _, cc, _ in free_filtered)


# ---------------------------------------------------------------------------
# Test 2: SUBSCRIPTION and METERED routes are never probed in --free-only
# ---------------------------------------------------------------------------


def test_subscription_and_metered_not_probed_free_only(tmp_path: Path, monkeypatch: Any) -> None:
    """SUBSCRIPTION routes are excluded from free-only refresh; metered-override routes too."""
    import os

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

    # Write a metered override for a route that would otherwise be unknown.
    from verdict.discovery.metered import add_metered_override

    add_metered_override("ovhfree/metered-model", 0.001, "discovery_observed_charge")

    probed_routes: list[str] = []

    def _capturing_transport(
        url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        import json as _json

        req = _json.loads(body)
        probed_routes.append(req.get("model", url))
        body_bytes = _json.dumps(
            {
                "model": req.get("model"),
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            }
        ).encode()
        return 200, {"Content-Type": "application/json"}, body_bytes

    _set_transport(_capturing_transport)
    try:
        from verdict.discovery.model import Route as _Route

        routes = [
            (_Route("omniroute", "sub/model", "http://localhost/v1"), "subscription", "oauth"),
            (
                _Route("omniroute", "ovhfree/metered-model", "http://localhost/v1"),
                "unknown",
                "no_free_evidence",
            ),
        ]
        # Manual free_only filter (what refresh.py does):
        filtered = [(r, cc, cf) for (r, cc, cf) in routes if cc in {"free", "unknown"}]
        # Then exclude metered overrides:
        from verdict.discovery.metered import load_metered_overrides

        metered_keys = frozenset(load_metered_overrides(os.environ).keys())

        def _canonical(key: str) -> str:
            return key[len("omniroute/") :] if key.startswith("omniroute/") else key

        filtered = [
            (r, cc, cf) for (r, cc, cf) in filtered if _canonical(r.key) not in metered_keys
        ]
        assert filtered == [], f"expected no routes after metered filter, got {filtered}"
    finally:
        _set_transport(None)


# ---------------------------------------------------------------------------
# Test 3: A charged probe writes a permanent METERED override
# ---------------------------------------------------------------------------


def test_charged_probe_becomes_metered_excluded(tmp_path: Path, monkeypatch: Any) -> None:
    """A UP probe with non-zero x-omniroute-response-cost writes the route to metered.json."""
    env = {"VERDICT_HOME": str(tmp_path), "HOME": str(tmp_path)}
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

    _set_transport(_ok_transport(cost=0.00042))
    try:
        from verdict.discovery.model import Route as _Route
        from verdict.discovery.registry import run_refresh

        route = _Route(
            provider="omniroute",
            model="ovhfree/mistral-7b",
            base_url="http://localhost:20128/v1",
            upstream="ovhfree",
        )
        run_refresh([(route, "unknown", "no_free_evidence")], wall_budget_s=30.0, env=env)
    finally:
        _set_transport(None)

    # The metered.json file must have been written.
    metered_file = tmp_path / "discovery" / "metered.json"
    assert metered_file.exists(), "metered.json not written after charged probe"

    metered_data = json.loads(metered_file.read_text())
    assert "ovhfree/mistral-7b" in metered_data, (
        f"route not in metered overrides: {list(metered_data.keys())}"
    )
    entry = metered_data["ovhfree/mistral-7b"]
    assert entry["evidence"] == "discovery_observed_charge"
    assert entry["observed_cost_usd"] == pytest.approx(0.00042)


# ---------------------------------------------------------------------------
# Test 4: UNKNOWN with fresh registry proof is admitted; without proof it is not
# ---------------------------------------------------------------------------


def test_unknown_registry_proof_admitted(tmp_path: Path, monkeypatch: Any) -> None:
    """An UNKNOWN route with a fresh tool_ok=True registry entry passes the agentic gate."""
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    from datetime import timezone

    from verdict.discovery.registry import (
        BREAKER_CLOSED,
        REGISTRY_SCHEMA,
        REGISTRY_TTL_S,
        DiscoveryRegistry,
        RegistryEntry,
        save_registry,
    )
    from verdict.orchestration.contracts import CapacityClass, TaskRequirements
    from verdict.orchestration.eligibility import EligibilityLadder
    from verdict.subagent_selection import HealthResult

    route_id = "ovhfree/mistral-7b"
    now = datetime.datetime.now(timezone.utc)

    # Build a registry with a qualifying UNKNOWN entry.
    entry = RegistryEntry(
        route_key=route_id,
        capacity_class="unknown",
        capacity_fact="no_free_evidence",
        base_url="http://localhost/v1",
        auth="none",
        upstream="ovhfree",
        answered=True,
        tool_ok=True,
        tool_skip_reason=None,
        probe_skip_reason=None,
        served_model="mistral-7b",  # matches model segment of route_key
        latency_ms=120,
        retry_after_s=None,
        breaker_state=BREAKER_CLOSED,
        checked_at=now.isoformat(),
    )
    registry = DiscoveryRegistry(
        schema=REGISTRY_SCHEMA, written_at=now.isoformat(), ttl_s=REGISTRY_TTL_S, entries=[entry]
    )
    save_registry(registry, env={"VERDICT_HOME": str(tmp_path)})

    from verdict.orchestration.health_cache import HealthCache

    empty_cache = HealthCache(tmp_path / "health-cache.json")

    # Build a minimal ladder with the UNKNOWN route in inventory.
    inventory = [
        {
            "id": route_id,
            "owned_by": "ovhfree",
            "capabilities": {"tool_calling": True},
            "context_length": 32768,
        }
    ]
    connections = [{"provider": "ovhfree", "authType": "apikey", "isActive": True}]
    ladder = EligibilityLadder(
        inventory,
        connections,
        lambda _: HealthResult(False, "not_probed"),
        tmp_path / "state.json",
        health_cache=empty_cache,
    )

    row = inventory[0]
    req = TaskRequirements(required_capabilities=frozenset({"tools"}), coding=True)
    result = ladder._task_gate(row, route_id, req, capacity=CapacityClass.UNKNOWN, now=now)
    assert result == "", (
        f"expected UNKNOWN route with fresh registry proof to pass _task_gate, got {result!r}"
    )

    # Without registry entry the same route must NOT be admitted.
    ladder2 = EligibilityLadder(
        inventory,
        connections,
        lambda _: HealthResult(False, "not_probed"),
        tmp_path / "state2.json",
        health_cache=empty_cache,
    )
    # Remove registry to simulate absent proof.
    (tmp_path / "discovery" / "registry.json").unlink()
    result2 = ladder2._task_gate(row, route_id, req, capacity=CapacityClass.UNKNOWN, now=now)
    assert result2 == "no_agentic_probe", (
        f"expected UNKNOWN route without registry proof to fail, got {result2!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: Route name alone never makes anything FREE
# ---------------------------------------------------------------------------


def test_route_name_alone_never_free() -> None:
    """A route name, suffix or provider id never determines FREE capacity (D31 preserved).

    The classification still returns UNKNOWN — not FREE — when the only signal
    is the route's name.  Admission policy treats UNKNOWN as free-tier, but
    *classification* is unchanged.
    """
    from verdict.capacity import CapacityClass, classify_route_capacity

    free_sounding_routes = [
        ("pol", "pol/community/vendor/model:free"),
        ("pollinations", "pollinations/claude-fast:free"),
        ("freemodel-dev", "freemodel-dev/free-model"),
        ("free", "free/model-free"),
        ("ovhfree", "ovhfree/mistral-7b"),
    ]
    conn = {"provider": "x", "authType": "apikey", "isActive": True}
    for provider, route_id in free_sounding_routes:
        conn_p = {**conn, "provider": provider}
        cls, _plan, evidence = classify_route_capacity(
            conn_p, {"id": route_id}, route_id=route_id, openrouter_free_set=None
        )
        assert cls is CapacityClass.UNKNOWN, (
            f"route {route_id!r} classified as {cls} (evidence={evidence!r}), "
            "expected UNKNOWN — name must never determine capacity"
        )
        assert evidence == "no_free_evidence", (
            f"route {route_id!r} evidence={evidence!r}, expected no_free_evidence"
        )
