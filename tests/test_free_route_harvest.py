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


def test_catalog_rows_from_payload_reads_openai_data_list() -> None:
    rows = catalog_rows_from_payload(
        {"object": "list", "data": [{"id": "oc/hy3:free"}, {"id": "paid/gpt"}]}
    )
    assert [row["id"] for row in rows] == ["oc/hy3:free", "paid/gpt"]
