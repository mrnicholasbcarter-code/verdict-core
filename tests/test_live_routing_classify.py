from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from verdict.live_routing import (
    ConcreteIdentity,
    LiveRoutingError,
    Mix,
    UsageSnapshot,
    classify_identities,
    explain,
    named_check_passes,
    select_route,
    strip_secrets,
)
from verdict.live_routing_run import run_live_routing


def _now() -> datetime:
    return datetime(2026, 8, 30, tzinfo=timezone.utc)


def _id(
    identity_id: str,
    *,
    cost: str | None = "free",
    provider: str = "p",
    captured: datetime | None = None,
    context: int | None = 128000,
    output: int | None = 4096,
    tools: bool | None = True,
    modalities: tuple[str, ...] | None = ("text",),
) -> ConcreteIdentity:
    return ConcreteIdentity(
        identity_id=identity_id,
        provider_id=provider,
        gateway_id="gw",
        cost_class=cost,  # type: ignore[arg-type]
        context_limit=context,
        output_limit=output,
        tools=tools,
        modalities=modalities,
        spec_captured_at=captured or _now(),
    )


def test_name_heuristic_does_not_qualify() -> None:
    item = _id("claude-3-opus", cost=None, context=None, output=None, tools=None, modalities=None)
    dropped = classify_identities([item], now=_now())
    assert dropped[0].status == "dropped"
    assert dropped[0].reason == "unclassified"


def test_denied_unclassified_stale_and_opaque() -> None:
    captured = _now()
    rows = [
        _id("denied-model"),
        _id("missing-specs", cost=None, context=None, output=None, tools=None, modalities=None),
        _id("stale-model", captured=captured - timedelta(hours=3)),
        _id("auto/best"),
        _id("good-free"),
    ]
    result = classify_identities(rows, denylist=frozenset({"denied-model"}), now=_now())
    reasons = {item.ref: item.reason for item in result if item.status == "dropped"}
    assert reasons["denied-model"] == "policy"
    assert reasons["missing-specs"] == "unclassified"
    assert reasons["stale-model"] == "stale"
    assert reasons["auto/best"] == "opaque_mix"
    kept = [item.ref for item in result if item.status == "kept"]
    assert kept == ["good-free"]


def test_cheaper_first_and_mix_paid_first_is_paid() -> None:
    paid = _id("paid-a", cost="paid")
    cheap = _id("free-a", cost="free")
    local = _id("local-a", cost="local")
    candidates = classify_identities([paid, cheap, local], now=_now())
    selection = select_route(candidates)
    assert selection.chosen.ref == "local-a"
    assert selection.paid_used is False
    assert selection.cheaper_available is True
    mix = Mix("combo", ("paid-a", "free-a"), opaque=False)
    mixed = classify_identities([paid, cheap], mixes=(mix,), now=_now())
    mix_candidate = next(item for item in mixed if item.ref == "combo")
    assert mix_candidate.status == "kept"
    assert mix_candidate.identity is not None
    assert mix_candidate.identity.cost_class == "paid"


def test_selection_is_deterministic() -> None:
    rows = [_id("free-b", cost="free"), _id("free-a", cost="free"), _id("paid-a", cost="paid")]
    first = select_route(classify_identities(rows, now=_now()))
    second = select_route(classify_identities(rows, now=_now()))
    assert first.chosen.ref == second.chosen.ref == "free-a"
    assert first.paid_used is False


def test_exhausted_quota_cannot_stay_cheaper() -> None:
    rows = [_id("codex-model", cost="free", provider="codex")]
    usage = [UsageSnapshot("codex", "oauth-file", 1.0, 0.0, None, True)]
    result = classify_identities(rows, usage=usage, now=_now())
    assert result[0].status == "dropped"
    assert result[0].reason == "quota"


def test_explanation_has_reasons_and_strips_secrets() -> None:
    rows = [_id("good-free"), _id("auto/best")]
    candidates = classify_identities(rows, now=_now())
    selection = select_route(candidates)
    payload = explain(candidates, selection)
    assert payload["chosen"] == "good-free"
    assert payload["paid_used"] is False
    assert any(item["reason"] == "opaque_mix" for item in payload["dropped"])
    dirty = {"token": "secret", "prompt": "hide", "kept": payload["kept"]}
    assert "token" not in strip_secrets(dirty)
    assert "prompt" not in strip_secrets(dirty)


def test_named_check_requires_exact_json() -> None:
    assert named_check_passes('{"golden_path": "ok"}')
    assert not named_check_passes('{"golden_path": "ok", "extra": true}')
    assert not named_check_passes("ok")


def test_mix_cost_class_is_first_remaining_qualified_step() -> None:
    paid = _id("paid-a", cost="paid", provider="codex")
    cheap = _id("free-a", cost="free", provider="other")
    mix = Mix("combo", ("paid-a", "free-a"), opaque=False)
    usage = [UsageSnapshot("codex", "oauth-file", 1.0, 0.0, None, True)]
    mixed = classify_identities([paid, cheap], mixes=(mix,), usage=usage, now=_now())
    combo = next(item for item in mixed if item.ref == "combo")
    assert combo.status == "kept"
    assert combo.identity is not None
    assert combo.identity.identity_id == "free-a"
    assert combo.identity.cost_class == "free"


def test_fixture_catalog_cannot_pass() -> None:
    with pytest.raises(LiveRoutingError) as exc:
        run_live_routing(catalog_source="provided")
    assert exc.value.code == "live_surface_blocked"
