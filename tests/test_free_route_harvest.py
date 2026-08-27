"""KEEP harvest + first-execute wait + best LIVE pick (free/concrete only)."""

from __future__ import annotations

from verdict.free_route_harvest import (
    TaskNeed,
    catalog_rows_from_payload,
    first_execute_need,
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


def test_catalog_rows_from_payload_reads_openai_data_list() -> None:
    rows = catalog_rows_from_payload(
        {"object": "list", "data": [{"id": "oc/hy3:free"}, {"id": "paid/gpt"}]}
    )
    assert [row["id"] for row in rows] == ["oc/hy3:free", "paid/gpt"]
