"""S1: one capacity classifier — failing-first tests.

Each test below documents one invariant from the S1 story:
  - capacity_of() is the public entrypoint, returning (class, deciding_fact).
  - Each allowed fact (gateway_free_tier, all_zero_pricing,
    import_free_only, no_connection) is covered.
  - plan_label is NOT a billing fact (CP2 §2.2 removed the rule).
  - A name (:free suffix) NEVER decides capacity in free_status or pick_best_live.
  - A no-connection route is never FREE.
  - gateway_free set format always matches route IDs (no double-prefix).

Tests marked FAILING-FIRST fail before the S1 fix and pass after.
Tests marked PASSING verify existing classify_route_capacity behavior.
"""

from __future__ import annotations

from verdict.capacity import CapacityClass, capacity_of, classify_route_capacity
from verdict.free_route_harvest import free_status, pick_best_live

# ---------------------------------------------------------------------------
# capacity_of() — public entrypoint (PASSING: existing classify_route_capacity
# already returns the deciding fact; capacity_of is the new wrapper)
# ---------------------------------------------------------------------------


def _conn(
    *, plan_label: str = "", auth_type: str = "apikey", import_free_only: bool = False
) -> dict[str, object]:
    return {"plan_label": plan_label, "authType": auth_type, "import_free_only": import_free_only}


def test_capacity_of_no_connection_is_unknown() -> None:
    """PASSING: no connection → UNKNOWN with deciding_fact 'no_connection'."""
    cls, fact = capacity_of(None, {}, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.UNKNOWN
    assert fact == "no_connection"


def test_capacity_of_plan_label_free_is_not_free() -> None:
    """CP2: plan_label is a name, not a billing fact — the rule was removed.
    A connection with plan_label containing 'free' but no other facts is UNKNOWN."""
    conn = _conn(plan_label="free_tier_plan")
    cls, fact = capacity_of(conn, {}, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.UNKNOWN
    assert fact == "no_free_evidence"


def test_capacity_of_import_free_only_is_free() -> None:
    """PASSING: import_free_only → FREE via import_free_only signal."""
    conn = _conn(import_free_only=True)
    cls, fact = capacity_of(conn, {}, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.FREE
    assert fact == "import_free_only"


def test_capacity_of_gateway_free_tier_is_free() -> None:
    """PASSING: gateway_free_tier flag on row → FREE via gateway_free_tier signal."""
    conn = _conn(auth_type="apikey")
    row = {"gateway_free_tier": True}
    cls, fact = capacity_of(conn, row, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.FREE
    assert fact == "gateway_free_tier"


def test_capacity_of_zero_pricing_is_free() -> None:
    """PASSING: explicit 0/0 pricing → FREE via all_zero_pricing signal."""
    conn = _conn(auth_type="apikey")
    row = {"pricing": {"input": 0, "output": 0}}
    cls, fact = capacity_of(conn, row, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.FREE
    assert fact == "all_zero_pricing"


def test_capacity_of_one_sided_zero_pricing_is_free() -> None:
    """PASSING: one-sided zero pricing (all declared values zero) → FREE."""
    conn = _conn(auth_type="apikey")
    row = {"pricing": {"input": 0}}
    cls, fact = capacity_of(conn, row, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.FREE
    assert fact == "all_zero_pricing"


def test_capacity_of_positive_pricing_is_metered() -> None:
    """PASSING: positive pricing → METERED."""
    conn = _conn(auth_type="apikey")
    row = {"pricing": {"input": 1.5, "output": 2.0}}
    cls, fact = capacity_of(conn, row, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.METERED
    assert fact == "apikey_positive_pricing"


def test_capacity_of_no_facts_is_unknown() -> None:
    """PASSING: no price facts, no free signal → UNKNOWN."""
    conn = _conn(auth_type="apikey", plan_label="standard")
    cls, fact = capacity_of(conn, {}, route_id="kc/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.UNKNOWN
    assert fact == "no_free_evidence"


def test_capacity_of_name_never_decides() -> None:
    """PASSING: a :free suffix in route_id never decides when conn is None."""
    cls, fact = capacity_of(None, {}, route_id="kc/some-model:free", openrouter_free_set=None)
    assert cls is CapacityClass.UNKNOWN
    assert fact == "no_connection"


def test_classify_route_capacity_returns_three_tuple() -> None:
    """PASSING: classify_route_capacity returns (class, plan_label, deciding_fact)."""
    result = classify_route_capacity(None, {}, route_id="kc/x", openrouter_free_set=None)
    assert len(result) == 3
    cls, plan_label, fact = result
    assert cls is CapacityClass.UNKNOWN
    assert plan_label == ""
    assert fact == "no_connection"


# ---------------------------------------------------------------------------
# free_status — name NEVER decides (FAILING-FIRST before S1 fix)
# ---------------------------------------------------------------------------


def test_free_status_name_alone_does_not_decide() -> None:
    """FAILING-FIRST: :free suffix without free_ids must return UNKNOWN, not 'free'.

    Currently fails because free_status returns 'free' for ':free' suffix even
    without free_ids. After S1 fix it returns 'UNKNOWN'.
    """
    row = {"id": "openrouter/nvidia/nemotron:free"}
    result = free_status(row)  # no free_ids
    assert result == "UNKNOWN", (
        f"free_status must not classify as free based on the route name alone; got {result!r}"
    )


def test_free_status_explicit_free_ids_decides() -> None:
    """PASSING: free_ids membership IS a fact and correctly decides 'free'."""
    row = {"id": "openrouter/nvidia/nemotron:free"}
    result = free_status(row, free_ids={"openrouter/nvidia/nemotron:free"})
    assert result == "free"


def test_free_status_positive_price_decides_paid_without_free_ids() -> None:
    """PASSING: positive pricing decides 'paid' when not in free_ids.

    free_ids is the gateway's authoritative declaration; it overrides catalog
    pricing (same semantics as gateway_free_tier overriding pricing in capacity_of).
    Without free_ids, positive pricing is the deciding fact.
    """
    row = {"id": "prov/model", "pricing": {"input": 1.0}}
    # no free_ids: positive pricing decides paid
    assert free_status(row) == "paid"
    # gateway declared free: free_ids wins over catalog pricing
    assert free_status(row, free_ids={"prov/model"}) == "free"


def test_free_status_no_facts_is_unknown() -> None:
    """PASSING: no price, no free_ids → UNKNOWN (name never decides)."""
    row = {"id": "prov/model-no-pricing"}
    assert free_status(row) == "UNKNOWN"


# ---------------------------------------------------------------------------
# pick_best_live — name NEVER decides (FAILING-FIRST before S1 fix)
# ---------------------------------------------------------------------------


def test_pick_best_live_name_alone_does_not_decide() -> None:
    """FAILING-FIRST: without free_ids, pick_best_live must return None even for
    routes whose name contains ':free'.

    Currently fails because pick_best_live returns a route based on the ':free'
    name. After S1 fix it returns None.
    """
    observations = [
        {"model_id": "fast/free:free", "availability_state": "ready", "latency_ms": 40.0},
        {"model_id": "slow/other:free", "availability_state": "ready", "latency_ms": 900.0},
    ]
    result = pick_best_live(observations)  # no free_ids
    assert result is None, (
        f"pick_best_live must not select a route based on its name alone; got {result!r}"
    )


def test_pick_best_live_free_ids_selects_minimum_latency() -> None:
    """PASSING: with free_ids, pick_best_live selects lowest-latency free route."""
    observations = [
        {"model_id": "prov/slow", "availability_state": "ready", "latency_ms": 900.0},
        {"model_id": "prov/fast", "availability_state": "ready", "latency_ms": 40.0},
        {"model_id": "prov/unavail", "availability_state": "unavailable", "latency_ms": 5.0},
    ]
    free_ids = {"prov/slow", "prov/fast", "prov/unavail"}
    result = pick_best_live(observations, free_ids=free_ids)
    assert result == "prov/fast"


def test_pick_best_live_no_free_ids_no_result() -> None:
    """PASSING (after fix): no free_ids → None (no fact basis for free selection)."""
    observations = [{"model_id": "prov/model", "availability_state": "ready", "latency_ms": 10.0}]
    # NOTE: before S1 fix, this would still return None unless ':free' in name.
    # The name-only test above covers the failing case.
    result = pick_best_live(observations, free_ids=set())
    assert result is None


# ---------------------------------------------------------------------------
# No-connection routes are never free or admitted as free
# ---------------------------------------------------------------------------


def test_no_connection_route_is_unknown_not_free() -> None:
    """PASSING: conn=None → UNKNOWN, confirming no-connection routes can't be FREE."""
    # Covers the cxa/ codex-app-server routes that have no backing connection.
    cls, fact = capacity_of(None, {}, route_id="cxa/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.UNKNOWN
    assert fact == "no_connection"


def test_no_connection_route_with_pricing_is_unknown() -> None:
    """PASSING: conn=None with zero pricing → still UNKNOWN (conn required for capacity)."""
    row = {"pricing": {"input": 0, "output": 0}, "gateway_free_tier": True}
    cls, fact = capacity_of(None, row, route_id="cxa/some-model", openrouter_free_set=None)
    assert cls is CapacityClass.UNKNOWN
    assert fact == "no_connection"


# ---------------------------------------------------------------------------
# gateway_free format: provider/model_id construction must match route IDs
# ---------------------------------------------------------------------------


def test_free_tier_model_route_id_no_double_prefix() -> None:
    """FAILING-FIRST (gateway_free format): FreeTierModel.route_id must not
    double-prefix when model_id already starts with the provider.

    Before the fix, f"{row.provider}/{row.model_id}" = "kc/kc/model" for a
    string-row entry where model_id = "kc/model" and provider = "kc".
    """
    from verdict.free_tier_admit import FreeTierModel

    # Case 1: model_id does NOT start with provider (normal dict row case)
    row = FreeTierModel(model_id="meta-llama/model:free", provider="kc")
    assert row.route_id == "kc/meta-llama/model:free", (
        f"expected 'kc/meta-llama/model:free', got {row.route_id!r}"
    )

    # Case 2: model_id ALREADY starts with provider/ (string-row case — the bug)
    row2 = FreeTierModel(model_id="kc/meta-llama/model:free", provider="kc")
    assert row2.route_id == "kc/meta-llama/model:free", (
        f"route_id must not double-prefix; expected 'kc/meta-llama/model:free', "
        f"got {row2.route_id!r}"
    )


def test_free_tier_model_route_id_normalizes_provider_case() -> None:
    """FAILING-FIRST (gateway_free format): provider must be lowercased in route_id."""
    from verdict.free_tier_admit import FreeTierModel

    row = FreeTierModel(model_id="meta-llama/model", provider="KC")
    assert row.route_id == "kc/meta-llama/model", (
        f"provider must be lowercase in route_id; got {row.route_id!r}"
    )


def test_free_tier_model_route_id_mixed_case_prefix_normalizes() -> None:
    """OCR finding: when model_id already carries the provider prefix but in a
    different case (provider='KC', model_id='KC/meta-llama/x:free'), route_id
    must lowercase the provider segment and not preserve the original casing.

    Before the fix, `return m` in the double-prefix branch returned
    'KC/meta-llama/x:free', which silently failed to match the catalog route id
    'kc/meta-llama/x:free' and caused the genuinely free route to be skipped.
    """
    from verdict.free_tier_admit import FreeTierModel

    # Case 3: model_id already starts with provider, but provider is uppercase
    row = FreeTierModel(model_id="KC/meta-llama/x:free", provider="KC")
    assert row.route_id == "kc/meta-llama/x:free", (
        f"provider prefix must be lowercased even when model_id already carries it; "
        f"got {row.route_id!r}"
    )

    # All three shapes now return a consistent, lowercase-provider route id
    assert (
        FreeTierModel(model_id="meta-llama/x:free", provider="kc").route_id
        == "kc/meta-llama/x:free"
    )
    assert (
        FreeTierModel(model_id="kc/meta-llama/x:free", provider="kc").route_id
        == "kc/meta-llama/x:free"
    )
    assert (
        FreeTierModel(model_id="KC/meta-llama/x:free", provider="KC").route_id
        == "kc/meta-llama/x:free"
    )
