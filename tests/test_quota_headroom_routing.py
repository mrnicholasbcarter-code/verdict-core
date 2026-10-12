"""Selection uses measured subscription quota: near-exhausted accounts are skipped."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.test_orch_eligibility import NOW, REQ, conn, make_ladder, row
from verdict.orchestration.run import quota_from_usage


def _quota(provider: str, pct: float | None = None, **extra: Any) -> dict[str, Any]:
    c = conn(provider)
    if pct is not None:
        c["quota_percent"] = pct
    c.update(extra)
    return c


CX, CC = "cx/same-model", "cc/same-model"


def _rows() -> list[dict[str, Any]]:
    return [row(CX, owned_by="codex"), row(CC, owned_by="claude")]


def _selected(ladder: Any) -> str:
    return str(ladder.select(REQ, now=NOW)[0].route_id)


def _verdict(ladder: Any, route: str) -> Any:
    return next(v for v in ladder.evaluate(REQ, now=NOW) if v.route_id == route)


def test_near_exhausted_account_is_not_selected(tmp_path: Path) -> None:
    ladder, _ = make_ladder(
        tmp_path,
        _rows(),
        [_quota("codex", 1.0, quota_reset_at="2026-02-03T00:00:00+00:00"), _quota("claude", 60.0)],
        prefer_providers=("codex",),
    )
    low = _verdict(ladder, CX)
    assert (low.failed_stage.value, low.reason) == ("AVAILABLE", "quota_low")
    assert low.cooldown_until == "2026-02-03T00:00:00+00:00"
    assert _selected(ladder) == CC


def test_more_remaining_quota_ranks_first_within_a_capacity_class(tmp_path: Path) -> None:
    ladder, _ = make_ladder(
        tmp_path,
        _rows(),
        [_quota("codex", 12.0), _quota("claude", 77.0)],
        prefer_providers=("codex",),
    )
    assert _selected(ladder) == CC


def test_unmeasured_quota_keeps_existing_ranking(tmp_path: Path) -> None:
    ladder, _ = make_ladder(
        tmp_path, _rows(), [_quota("codex"), _quota("claude")], prefer_providers=("codex",)
    )
    assert _selected(ladder) == CX


def test_per_model_window_blocks_only_that_model(tmp_path: Path) -> None:
    rows = [
        row("agy/gemini-3.1-pro-high", owned_by="agy"),
        row("agy/claude-sonnet-4-6", owned_by="agy"),
    ]
    windows = {"gemini-3.1-pro-high": 0.0, "claude-sonnet-4-6": 90.0}
    ladder, _ = make_ladder(tmp_path, rows, [_quota("agy", quota_models=windows)])
    assert _verdict(ladder, "agy/gemini-3.1-pro-high").reason == "quota_low"
    other = _verdict(ladder, "agy/claude-sonnet-4-6")
    assert other.reason != "quota_low" and other.rank_components["quota_percent"] == 90.0


def test_route_uses_the_account_with_the_most_quota(tmp_path: Path) -> None:
    ladder, _ = make_ladder(tmp_path, _rows()[:1], [_quota("codex", 1.0), _quota("codex", 40.0)])
    verdict = _verdict(ladder, CX)
    assert verdict.reason != "quota_low" and verdict.rank_components["quota_percent"] == 40.0


def test_usage_body_parsing_matches_the_gateway_shapes() -> None:
    codex = {
        "quotas": {
            "session": {
                "used": 34,
                "total": 100,
                "remaining": 66,
                "resetAt": "2026-10-12T17:02:45Z",
            },
            "weekly": {"used": 99, "total": 100, "remaining": 1, "resetAt": "2026-10-18T04:04:59Z"},
        }
    }
    assert quota_from_usage(codex) == {
        "quota_percent": 1.0,
        "quota_reset_at": "2026-10-18T04:04:59Z",
    }
    agy = {
        "quotas": {
            "gemini-3.1-pro-high": {"total": 1000, "remainingPercentage": 0, "used": 1000},
            "claude-sonnet-4-6": {"total": 1000, "remainingPercentage": 99.9, "used": 1},
            "credits": {"unlimited": True},
        }
    }
    assert quota_from_usage(agy) == {
        "quota_models": {"gemini-3.1-pro-high": 0.0, "claude-sonnet-4-6": 99.9}
    }
    assert quota_from_usage({"error": "Usage not available"}) == {}
