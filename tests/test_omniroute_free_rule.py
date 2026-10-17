"""Failing specs for the unified OmniRoute discovery free rule (spec v6.1 section 0.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from verdict.omniroute_pipeline.free_rule import (
    FreeStatus,
    ProviderFreeFlags,
    classify_free,
    load_free_flags,
    parse_kilo_catalog,
    parse_openrouter_catalog,
    write_free_flags,
)

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _flags(entries: dict[str, dict[str, bool | float | None]]) -> ProviderFreeFlags:
    return ProviderFreeFlags(fetched_at=NOW, entries=entries)


def test_free_rule_zero_price_or_provider_flag() -> None:
    assert classify_free("nvidia/z-ai/glm-5.3", {"input": 0, "output": 0}, None) is FreeStatus.FREE
    flags = _flags({"kc/reflection/beam-free": {"is_free": True}})
    assert classify_free("kc/reflection/beam-free", None, flags) is FreeStatus.FREE


def test_nonzero_any_source_not_free() -> None:
    flags = _flags({"kc/google/gemini-4-argon": {"is_free": True, "input": 2.0, "output": 10.0}})
    assert (
        classify_free("kc/google/gemini-4-argon", {"input": 0, "output": 0}, flags)
        is FreeStatus.NOT_FREE
    )
    assert (
        classify_free("nvidia/nvidia/nemotron-3-ultra", {"input": 0.5, "output": 2.5}, None)
        is FreeStatus.NOT_FREE
    )


def test_blank_without_flag_fails_closed() -> None:
    assert (
        classify_free("agentrouter/deepseek-v4-flash", None, _flags({}))
        is FreeStatus.UNVERIFIED_FREE
    )
    assert classify_free("kc/glm-5.2:free", {}, _flags({})) is FreeStatus.UNVERIFIED_FREE


def test_kc_lookalike_without_free_suffix_paid() -> None:
    flags = _flags(
        {
            "kc/nvidia/nemotron-3.5-super-vl:free": {"is_free": True},
            "kc/nvidia/nemotron-3.5-super-vl": {"is_free": False, "input": 0.1, "output": 0.4},
        }
    )
    assert classify_free("kc/nvidia/nemotron-3.5-super-vl:free", None, flags) is FreeStatus.FREE
    assert classify_free("kc/nvidia/nemotron-3.5-super-vl", None, flags) is FreeStatus.NOT_FREE


def test_free_suffix_or_omniroute_isfree_alone_is_not_evidence() -> None:
    assert (
        classify_free("x/some-model:free", None, None, omniroute_is_free=True)
        is FreeStatus.UNVERIFIED_FREE
    )


def test_provider_catalog_parsers_exact_ids() -> None:
    kilo = parse_kilo_catalog(
        {
            "data": [
                {
                    "id": "reflection/beam-free",
                    "isFree": True,
                    "pricing": {"prompt": "0", "completion": "0"},
                }
            ]
        },
        prefix="kc",
    )
    assert kilo["kc/reflection/beam-free"]["is_free"] is True
    orr = parse_openrouter_catalog(
        {
            "data": [
                {"id": "a/b", "pricing": {"prompt": "0", "completion": "0"}},
                {"id": "a/c", "pricing": {"prompt": "0.000001", "completion": "0"}},
            ]
        },
        prefix="openrouter",
    )
    assert orr["openrouter/a/b"]["is_free"] is True
    assert orr["openrouter/a/c"]["is_free"] is False


def test_free_flag_last_good_expires_48h(tmp_path: Path) -> None:
    path = tmp_path / "free-flags-last-good.json"
    write_free_flags(path, _flags({"kc/reflection/beam-free": {"is_free": True}}))
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    fresh = load_free_flags(path, now=NOW + timedelta(hours=47))
    assert fresh is not None
    assert classify_free("kc/reflection/beam-free", None, fresh) is FreeStatus.FREE
    assert load_free_flags(path, now=NOW + timedelta(hours=49)) is None
    assert classify_free("kc/reflection/beam-free", None, None) is FreeStatus.UNVERIFIED_FREE
