"""BOD-418: OpenRouter FREE membership from public /models pricing."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.openrouter_free_set import (
    DEFAULT_TTL_SECONDS,
    OpenRouterFreeSet,
    intersect_with_gateway_ids,
    key_limit_on_non_free_is_route_scoped,
    load_openrouter_free_set,
    openrouter_route_is_public_free,
    parse_free_entries,
    pricing_is_explicitly_free,
)
from verdict.orchestration.contracts import CapacityClass
from verdict.orchestration.eligibility import EligibilityLadder, capacity_class_of
from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.prove_at_rest import _cooled_pools, _detail_is_model_scoped

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _row(
    model_id: str,
    *,
    prompt: object = "0",
    completion: object = "0",
    extra_pricing: dict[str, object] | None = None,
    output_modalities: list[str] | None = None,
) -> dict[str, Any]:
    pricing: dict[str, object] = {"prompt": prompt, "completion": completion}
    if extra_pricing:
        pricing.update(extra_pricing)
    arch = {
        "modality": "text->text",
        "input_modalities": ["text"],
        "output_modalities": output_modalities or ["text"],
    }
    return {"id": model_id, "pricing": pricing, "architecture": arch}


def _payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": rows}


def test_default_ttl_is_six_hours() -> None:
    assert DEFAULT_TTL_SECONDS == 6 * 60 * 60


def test_pricing_requires_explicit_numeric_zero_prompt_and_completion() -> None:
    assert pricing_is_explicitly_free({"prompt": "0", "completion": 0})
    assert not pricing_is_explicitly_free({"prompt": "0"})  # missing completion
    assert not pricing_is_explicitly_free({"prompt": None, "completion": 0})
    assert not pricing_is_explicitly_free({"prompt": "free", "completion": "0"})
    assert not pricing_is_explicitly_free({"prompt": "0", "completion": "0.1"})
    assert not pricing_is_explicitly_free({"prompt": "0", "completion": "0", "request": "0.01"})


def test_stale_free_suffix_not_in_public_set() -> None:
    payload = _payload(
        [
            _row("poolside/laguna-xs-2.1:free")
            # stale :free id absent from public zero set on purpose
        ]
    )
    entries = parse_free_entries(payload)
    assert "poolside/laguna-xs-2.1:free" in entries
    assert "meta-llama/llama-3.3-70b-instruct:free" not in entries
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    assert not openrouter_route_is_public_free(
        "openrouter/meta-llama/llama-3.3-70b-instruct:free", free_set
    )
    assert openrouter_route_is_public_free("openrouter/poolside/laguna-xs-2.1:free", free_set)


def test_paid_ids_are_not_free_even_with_free_suffix_or_connection_signal() -> None:
    payload = _payload(
        [
            _row("aion-labs/aion-3.5-mini", prompt="0.1", completion="0.2"),
            _row("vendor/looks-free:free", prompt="0.5", completion="0"),
        ]
    )
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(parse_free_entries(payload)),
        entries=parse_free_entries(payload),
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    assert not openrouter_route_is_public_free("openrouter/aion-labs/aion-3.5-mini", free_set)
    assert not openrouter_route_is_public_free("openrouter/vendor/looks-free:free", free_set)


def test_malformed_and_missing_pricing_are_not_zero() -> None:
    rows = [
        {"id": "x/missing", "architecture": {"output_modalities": ["text"]}},
        _row("x/bad", prompt="n/a", completion="0"),
        _row("x/ok"),
    ]
    entries = parse_free_entries(_payload(rows))
    assert set(entries) == {"x/ok"}


def test_fetch_failure_without_valid_cache_yields_no_free_assertion(tmp_path: Path) -> None:
    def boom(url: str, timeout: float) -> dict[str, Any]:
        raise TimeoutError("no network")

    result = load_openrouter_free_set(
        now=NOW, cache_path=tmp_path / "cache.json", fetch=boom, ttl_seconds=60
    )
    assert result is None


def test_fetch_failure_reuses_still_valid_cache(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    live = load_openrouter_free_set(
        now=NOW,
        cache_path=path,
        ttl_seconds=3600,
        fetch=lambda url, timeout: _payload([_row("poolside/laguna-xs-2.1:free")]),
    )
    assert live is not None and live.contains("poolside/laguna-xs-2.1:free")

    def boom(url: str, timeout: float) -> dict[str, Any]:
        raise OSError("down")

    cached = load_openrouter_free_set(
        now=NOW + timedelta(minutes=5), cache_path=path, ttl_seconds=3600, fetch=boom
    )
    assert cached is not None
    assert cached.source == "cache"
    assert cached.contains("poolside/laguna-xs-2.1:free")


def test_opaque_free_router_alias_is_not_certified_free() -> None:
    entries = parse_free_entries(_payload([_row("openrouter/free")]))
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    assert not openrouter_route_is_public_free("openrouter/free", free_set)
    assert "openrouter/free" not in free_set.coding_candidate_ids()


def test_non_text_output_modality_excluded_from_coding_candidates() -> None:
    entries = parse_free_entries(
        _payload(
            [
                _row("google/lyria-3-pro-preview", output_modalities=["text", "audio"]),
                _row("poolside/laguna-xs-2.1:free", output_modalities=["text"]),
            ]
        )
    )
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    coding = free_set.coding_candidate_ids()
    assert "poolside/laguna-xs-2.1:free" in coding
    assert "google/lyria-3-pro-preview" not in coding


def test_intersect_with_gateway_ids_keeps_only_public_free() -> None:
    entries = parse_free_entries(_payload([_row("poolside/laguna-xs-2.1:free")]))
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    gateway = {
        "openrouter/poolside/laguna-xs-2.1:free",
        "openrouter/aion-labs/aion-3.5-mini",
        "openrouter/meta-llama/llama-3.3-70b-instruct:free",
        "cc/claude-sonnet-4-6",
    }
    assert intersect_with_gateway_ids(free_set, gateway) == {
        "openrouter/poolside/laguna-xs-2.1:free"
    }


def test_capacity_class_openrouter_suffix_alone_is_not_free() -> None:
    """OpenRouter-family :free without public set membership is not FREE."""
    conn = {
        "provider": "openrouter",
        "authType": "apikey",
        "isActive": True,
        "testStatus": "ok",
        "plan_label": "free-key",
        "import_free_only": True,
    }
    row = {
        "id": "openrouter/aion-labs/aion-3.5-mini",
        "owned_by": "openrouter",
        "capabilities": {"tool_calling": True},
        "context_length": 128000,
    }
    # Without a public free set wired, openrouter routes must not become FREE
    # from suffix / import_free_only alone once BOD-418 is applied.
    capacity, _plan = capacity_class_of(conn, row)
    # capacity_class_of historically returned FREE via import_free_only; the
    # BOD-418 wiring must stop that for openrouter-family routes.
    assert capacity is not CapacityClass.FREE


def test_ladder_openrouter_free_suffix_requires_public_membership(tmp_path: Path) -> None:
    from verdict.subagent_selection import HealthResult

    conn = {
        "provider": "openrouter",
        "authType": "apikey",
        "isActive": True,
        "testStatus": "ok",
        "plan_label": "payg",
        "import_free_only": False,
    }
    paid = {
        "id": "openrouter/aion-labs/aion-3.5-mini:free",
        "owned_by": "openrouter",
        "capabilities": {"tool_calling": True, "chat": True},
        "context_length": 128000,
    }
    genuine = {
        "id": "openrouter/poolside/laguna-xs-2.1:free",
        "owned_by": "openrouter",
        "capabilities": {"tool_calling": True, "chat": True},
        "context_length": 128000,
    }
    state_path = tmp_path / "ladder-state.json"
    state_path.write_text("{}")
    entries = parse_free_entries(_payload([_row("poolside/laguna-xs-2.1:free")]))
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    ladder = EligibilityLadder(
        inventory_rows=[paid, genuine],
        connections=[conn],
        probe=lambda _rid: HealthResult(healthy=True, category="", status_code=200),
        state_path=state_path,
        openrouter_free_set=free_set,
    )
    cap_paid, _, _evidence_paid = ladder._capacity_class(conn, paid, route_id=paid["id"])
    cap_free, _, evidence_free = ladder._capacity_class(conn, genuine, route_id=genuine["id"])
    assert cap_paid is not CapacityClass.FREE
    assert cap_free is CapacityClass.FREE
    assert evidence_free == "openrouter_public_zero"


def test_paid_model_key_limit_does_not_cool_free_pool(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "cache.json", bucket_capacity=100)
    paid = "openrouter/aion-labs/aion-3.5-mini"
    detail = (
        f"[{paid}] [403]: Key limit exceeded (total limit). "
        "Manage it using https://openrouter.ai/settings/keys"
    )
    entries = parse_free_entries(_payload([_row("poolside/laguna-xs-2.1:free")]))
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    assert key_limit_on_non_free_is_route_scoped(
        route_id=paid, http_status=403, detail=detail, free_set=free_set
    )
    # Recording with route scope must leave the openrouter-free pool usable.
    cache.record(
        paid,
        ProbeResult(
            "permission",
            False,
            False,
            http_status=403,
            pool="openrouter-free",
            failure_scope="route",
        ),
        NOW,
    )
    assert "openrouter-free" not in _cooled_pools(cache, NOW)


def test_genuine_free_account_key_limit_still_blocks() -> None:
    route_id = "openrouter/poolside/laguna-xs-2.1:free"
    detail = f"[{route_id}] [403]: Key limit exceeded (total limit)"
    entries = parse_free_entries(_payload([_row("poolside/laguna-xs-2.1:free")]))
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    assert not key_limit_on_non_free_is_route_scoped(
        route_id=route_id, http_status=403, detail=detail, free_set=free_set
    )
    # Account-level key limit on a confirmed-free route stays provider-scoped.
    assert not _detail_is_model_scoped(
        "permission", 403, detail, route_id, openrouter_free_set=free_set
    )


def test_routes_from_evidence_marks_only_public_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict import prove_at_rest
    from verdict.admission import _MINT, AdmissionRecord, AdmittedSet

    entries = parse_free_entries(_payload([_row("poolside/laguna-xs-2.1:free")]))
    free_set = OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    connections = [
        {
            "provider": "openrouter",
            "authType": "apikey",
            "isActive": True,
            "testStatus": "ok",
            "plan_label": "free",
            "import_free_only": True,
        }
    ]
    rows = [
        {
            "id": "openrouter/poolside/laguna-xs-2.1:free",
            "owned_by": "openrouter",
            "capabilities": {"tool_calling": True, "chat": True},
            "context_length": 128000,
        },
        {
            "id": "openrouter/aion-labs/aion-3.5-mini",
            "owned_by": "openrouter",
            "capabilities": {"tool_calling": True, "chat": True},
            "context_length": 128000,
        },
        {
            "id": "openrouter/meta-llama/llama-3.3-70b-instruct:free",
            "owned_by": "openrouter",
            "capabilities": {"tool_calling": True, "chat": True},
            "context_length": 128000,
        },
    ]

    def fake_admit(inventory, connections, evidence, *, now, **kwargs):
        records = tuple(
            AdmissionRecord(
                route_id=str(row["id"]),
                provider="openrouter",
                admitted=True,
                first_failed_stage=None,
                reason="",
                source="test",
                observed_at=now.isoformat(),
                health="unknown",
            )
            for row in inventory
        )
        return AdmittedSet(
            records=records,
            generated_at=now.isoformat(),
            sources=("test",),
            controller_identity="test",
            narrowing=(),
            runtime_consulted=(),
            _mint=_MINT,
        )

    monkeypatch.setattr("verdict.admission.admit", fake_admit)
    routes = prove_at_rest.routes_from_evidence(
        rows, connections, now=NOW, state_dir=tmp_path, openrouter_free_set=free_set
    )
    by_id = {route.route_id: route for route in routes}
    assert by_id["openrouter/poolside/laguna-xs-2.1:free"].capacity == "free"
    assert by_id["openrouter/aion-labs/aion-3.5-mini"].capacity != "free"
    assert by_id["openrouter/meta-llama/llama-3.3-70b-instruct:free"].capacity != "free"
