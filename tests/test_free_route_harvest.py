"""KEEP harvest + first-execute wait + best LIVE pick (free/concrete only)."""

from __future__ import annotations

from verdict.free_route_harvest import (
    TaskNeed,
    catalog_rows_from_payload,
    first_execute_need,
    harvest_live_route,
    keep_free_compatible,
    pick_best_live,
)


def _row(
    model_id: str,
    *,
    owned_by: str = "openrouter",
    capabilities: dict[str, bool] | None = None,
    pricing: dict[str, float] | None = None,
    context_length: int | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {"id": model_id, "owned_by": owned_by}
    if capabilities is not None:
        row["capabilities"] = capabilities
    if pricing is not None:
        row["pricing"] = pricing
    if context_length is not None:
        row["context_length"] = context_length
    return row


CHAT = {"chat": True, "tools": True}
EMBED = {"embedding": True}


def test_keep_drops_combo_internal_paid_and_wrong_modality() -> None:
    rows = [
        _row("openrouter/nvidia/nemotron-3:free", capabilities=CHAT),
        _row("openrouter/poolside/laguna-s-2.1:free", capabilities=CHAT),
        _row("paid/gpt-4o", capabilities=CHAT, pricing={"prompt": 2.5}),
        _row("auto/best-free", capabilities=CHAT),
        _row("gopus", owned_by="combo", capabilities=CHAT),
        _row("openrouter/free", capabilities=CHAT),
        _row("openrouter/text-embed:free", capabilities=EMBED),
        _row("combo/coding", capabilities=CHAT),
    ]
    keep = keep_free_compatible(rows, TaskNeed(chat=True))
    assert keep == ["openrouter/nvidia/nemotron-3:free", "openrouter/poolside/laguna-s-2.1:free"]


def test_keep_fails_open_when_capabilities_omitted() -> None:
    rows = [_row("oc/hy3-free"), _row("cc/claude-sonnet-5", pricing={"prompt": 3.0})]
    keep = keep_free_compatible(rows, TaskNeed(chat=True))
    assert keep == ["oc/hy3-free"]


def test_keep_drops_stated_context_too_small() -> None:
    rows = [
        _row("oc/small:free", capabilities=CHAT, context_length=1024),
        _row("oc/big:free", capabilities=CHAT, context_length=128_000),
        _row("oc/unknown:free", capabilities=CHAT),
    ]
    keep = keep_free_compatible(rows, TaskNeed(chat=True, min_context=32_000))
    assert keep == ["oc/big:free", "oc/unknown:free"]


def test_first_execute_need_is_min_keep_max_10pct_catalog_25pct_keep() -> None:
    assert first_execute_need(catalog_n=200, keep_n=40) == 20
    assert first_execute_need(catalog_n=100, keep_n=80) == 20
    assert first_execute_need(catalog_n=10, keep_n=3) == 3
    assert first_execute_need(catalog_n=0, keep_n=0) == 0


def test_pick_best_live_is_min_latency_free_not_first_ready() -> None:
    observations = [
        {"model_id": "slow/free:free", "availability_state": "ready", "latency_ms": 900.0},
        {"model_id": "fast/free:free", "availability_state": "ready", "latency_ms": 40.0},
        {"model_id": "paid/gpt", "availability_state": "ready", "latency_ms": 10.0},
        {"model_id": "dead/free:free", "availability_state": "unavailable", "latency_ms": 5.0},
    ]
    assert pick_best_live(observations) == "fast/free:free"


def test_keep_excludes_stated_missing_tool_calling_when_unit_requires_it() -> None:
    """FR-030: predicates come from the unit. Stated absence excludes; omission fails open."""
    rows = [
        _row("oc/no-tools:free", capabilities={"chat": True, "tools": False}),
        _row("oc/has-tools:free", capabilities={"chat": True, "tools": True}),
        _row("oc/unstated:free", capabilities={"chat": True}),
    ]
    keep = keep_free_compatible(rows, TaskNeed(chat=True, tools=True))
    assert keep == ["oc/has-tools:free", "oc/unstated:free"]


def test_keep_without_tool_requirement_keeps_non_tool_routes() -> None:
    """A unit that needs no tools must not have tool calling imposed on it."""
    rows = [_row("oc/no-tools:free", capabilities={"chat": True, "tools": False})]
    assert keep_free_compatible(rows, TaskNeed(chat=True)) == ["oc/no-tools:free"]


def test_keep_uses_token_budget_as_context_floor() -> None:
    """FR-030: the unit's declared token budget is the context predicate."""
    rows = [
        _row("oc/tiny:free", capabilities=CHAT, context_length=2048),
        _row("oc/roomy:free", capabilities=CHAT, context_length=200_000),
    ]
    keep = keep_free_compatible(rows, TaskNeed(chat=True, token_budget=8192))
    assert keep == ["oc/roomy:free"]


def test_keep_excludes_stated_missing_modality_when_unit_requires_it() -> None:
    rows = [
        _row("oc/text-only:free", capabilities={"chat": True, "vision": False}),
        _row("oc/sees:free", capabilities={"chat": True, "vision": True}),
    ]
    keep = keep_free_compatible(rows, TaskNeed(chat=True, modality="vision"))
    assert keep == ["oc/sees:free"]


def test_keep_retains_real_catalog_free_rows_that_never_state_chat() -> None:
    """Live catalogs advertise tools/vision/reasoning but no `chat` key. Those are the
    free models the whole harvest exists to find; an absent `chat` must never drop them."""
    row = _row(
        "openrouter/inclusionai/ling-3.0-flash:free",
        capabilities={
            "vision": False,
            "pdf": False,
            "audioInput": False,
            "tools": True,
            "reasoning": True,
            "contextWindow": 128_000,
        },
        context_length=128_000,
    )
    keep = keep_free_compatible([row], TaskNeed(chat=True, tools=True, token_budget=8192))
    assert keep == ["openrouter/inclusionai/ling-3.0-flash:free"]


def test_keep_drops_opaque_alias_in_any_namespace() -> None:
    """`kr/auto` is a gateway resolver alias, not a concrete route (FR-008)."""
    rows = [
        _row("kr/auto", owned_by="kr", capabilities={"thinking": False}),
        _row("kr/auto-thinking", owned_by="kr", capabilities={"thinking": True}),
        _row("kr/claude-sonnet-4.5", owned_by="kr", capabilities={"thinking": False}),
    ]
    assert keep_free_compatible(rows, TaskNeed(chat=True)) == ["kr/claude-sonnet-4.5"]


def test_no_alias_or_combo_survives_live_catalog_shapes() -> None:
    """FR-035: an alias in any segment is never selectable, whatever the catalog claims.

    Shapes taken from the live gateway catalog: `kr/auto` advertises a 200k context and
    `gopus`/`co` are bare combo entries. Rich advertised capability must not rescue them.
    """
    rows = [
        _row("kr/auto", owned_by="kr", capabilities={"tools": True}, context_length=200_000),
        _row("kr/auto-thinking", owned_by="kr", capabilities={"tools": True}),
        _row("gopus", owned_by="combo", capabilities={"tools": True}),
        _row("co", owned_by="combo", capabilities={"tools": True}),
        _row("auto/best", capabilities={"tools": True}),
        _row("openrouter/pool/real-model:free", capabilities={"tools": True}),
    ]
    keep = keep_free_compatible(rows, TaskNeed(chat=True, tools=True))
    assert keep == ["openrouter/pool/real-model:free"]


def test_catalog_alone_never_yields_an_executable_route() -> None:
    """FR-034: with no probe evidence, harvest admits nothing for execution."""
    rows = [_row("openrouter/real:free", capabilities={"tools": True})]

    def dead_transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        del model_id, payload, timeout
        return {"status_code": 503, "body": {}}

    assert harvest_live_route(rows, dead_transport, TaskNeed(chat=True)) == {}


def test_free_status_is_unknown_when_no_facet_and_no_marker() -> None:
    """FR-036: absent pricing is not evidence of free. Live catalogs omit pricing entirely."""
    from verdict.free_route_harvest import free_status

    assert free_status(_row("oc/plain-model")) == "UNKNOWN"
    assert free_status(_row("oc/model:free")) == "free"
    assert free_status(_row("paid/gpt", pricing={"prompt": 2.5})) == "paid"


def test_free_status_uses_adapter_facet_when_supplied() -> None:
    """A gateway that positively declares free-ness beats id-shape inference."""
    from verdict.free_route_harvest import free_status

    assert free_status(_row("oc/plain"), free_ids={"oc/plain"}) == "free"
    # facet present but silent about this id: still UNKNOWN, never assumed free
    assert free_status(_row("oc/other"), free_ids={"oc/plain"}) == "UNKNOWN"


def test_positively_free_ids_are_preferred_over_unknown() -> None:
    """FR-036: UNKNOWN stays probe-eligible but must not outrank a positively-free id."""
    rows = [_row("oc/unknown-cost"), _row("oc/known:free")]
    keep = keep_free_compatible(rows, TaskNeed(chat=True))
    assert keep == ["oc/known:free", "oc/unknown-cost"]
    assert set(keep) == {"oc/known:free", "oc/unknown-cost"}


def test_free_marker_does_not_rescue_an_opaque_alias() -> None:
    """FR-035: `bzl/auto:free` is still a resolver alias. Found live on OmniRoute :20128.

    The alias leaf carried a `:free` suffix, so leaf-based alias detection missed it and a
    combo route was admitted as if it were a concrete free model.
    """
    rows = [
        _row("bzl/auto:free", capabilities={"tools": True}),
        _row("bazaarlink/auto:free", capabilities={"tools": True}),
        _row("bzl/auto-thinking:free", capabilities={"tools": True}),
        _row("bzl/qwen/qwen3.7-flash:free", capabilities={"tools": True}),
    ]
    assert keep_free_compatible(rows, TaskNeed(chat=True)) == ["bzl/qwen/qwen3.7-flash:free"]


def test_catalog_rows_from_payload_reads_openai_data_list() -> None:
    rows = catalog_rows_from_payload(
        {"object": "list", "data": [{"id": "oc/hy3:free"}, {"id": "paid/gpt"}]}
    )
    assert [row["id"] for row in rows] == ["oc/hy3:free", "paid/gpt"]


def test_affordability_excludes_route_that_cannot_finish_the_unit() -> None:
    """FR-029: observed capacity below the unit's estimated cost is an admission floor.

    Having *some* capacity is not evidence a route can finish the work; the whole point of
    the estimate is that a route which dies mid-unit wastes the attempt.
    """
    rows = [_row("oc/tiny-left:free"), _row("oc/plenty:free")]
    keep = keep_free_compatible(
        rows,
        TaskNeed(chat=True, token_budget=8000),
        remaining_tokens={"oc/tiny-left:free": 100, "oc/plenty:free": 50_000},
    )
    assert keep == ["oc/plenty:free"]


def test_unobserved_capacity_never_excludes() -> None:
    """FR-029/SC-008: no observation means UNKNOWN, which must not exclude."""
    rows = [_row("oc/unobserved:free")]
    keep = keep_free_compatible(rows, TaskNeed(chat=True, token_budget=8000), remaining_tokens={})
    assert keep == ["oc/unobserved:free"]


def test_same_kept_set_whether_or_not_the_gateway_declares_a_free_facet() -> None:
    """FR-038 conformance: an optional facet may reorder, never change admission.

    A gateway exposing a free-tier catalog (OmniRoute) and one exposing nothing must
    admit the same identities, or Verdict would be locked to a single vendor.
    """
    rows = [_row("oc/a"), _row("oc/b"), _row("oc/c:free")]
    need = TaskNeed(chat=True)
    facet_rich = keep_free_compatible(rows, need, free_ids={"oc/a"})
    facet_free = keep_free_compatible(rows, need)
    assert set(facet_rich) == set(facet_free) == {"oc/a", "oc/b", "oc/c:free"}
    # the facet is used when present: a declared-free id ranks ahead of UNKNOWN
    assert facet_rich[0] == "oc/a"
    assert facet_free[0] == "oc/c:free"
