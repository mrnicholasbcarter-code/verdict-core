"""A provider is out of quota only when every usable connection of it is (issue #1416).

The usage bodies below have the shape the gateway returned for the four Codex
connections on 2026-11-28: three Plus connections whose ``session`` and
``weekly`` windows carry only ``remainingPercentage``, and one free connection
whose only window (``unknown_primary``) was used up until 2026-12-08. Ids are
made up. No test here calls the gateway.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import NOW, REQ, conn, make_ladder, row
from verdict.orchestration.eligibility import quota_remaining
from verdict.orchestration.run import quota_from_usage

CX, CODEX = "cx/gpt-5.5", "codex/gpt-5.5-mini"
RESET = "2026-12-08T09:45:13.000Z"
RESET_ISO = "2026-12-08T09:45:13+00:00"
# The tests run at 2026-02-01 (``NOW``); every reset below is in the future of it.


def _plus_usage(session: float, weekly: float) -> dict[str, Any]:
    return {
        "quotas": {
            "session": {"remainingPercentage": session},
            "weekly": {"remainingPercentage": weekly},
        }
    }


def _free_usage_exhausted(reset: str = RESET) -> dict[str, Any]:
    return {
        "quotas": {
            "unknown_primary": {
                "displayName": "Monthly",
                "used": 100,
                "total": 100,
                "remaining": 0,
                "remainingPercentage": 0,
                "windowSeconds": 2592000,
                "resetAt": reset,
            }
        }
    }


def _account(
    label: str, usage: dict[str, Any] | None = None, *, plan: str = "plus", **overrides: Any
) -> dict[str, Any]:
    account = conn("codex", plan=plan)
    account["id"] = account["account_id"] = f"conn-{label}"
    if usage is not None:
        account.update(quota_from_usage(usage))
    account.update(overrides)
    return account


def _rows() -> list[dict[str, Any]]:
    return [row(CX, owned_by="codex"), row(CODEX, owned_by="codex")]


def _outcome(verdict: Any) -> tuple[str | None, str]:
    stage = verdict.failed_stage
    return (stage.value if stage is not None else None, verdict.reason)


def _verdicts(tmp_path: Path, accounts: list[dict[str, Any]]) -> dict[str, Any]:
    ladder, _ = make_ladder(tmp_path, _rows(), accounts)
    return {v.route_id: v for v in ladder.evaluate(REQ, now=NOW)}


def test_recorded_usage_bodies_read_as_the_gateway_returned_them() -> None:
    """Pin what the recorded bodies mean to Verdict, so the tests below stay honest.

    Plus windows with only ``remainingPercentage`` are kept as named windows, not
    as the account's quota: for a Codex route that connection's quota is unknown.
    """
    plus = _account("plus-1", _plus_usage(99, 100))
    assert plus["quota_models"] == {"session": 99.0, "weekly": 100.0}
    assert quota_remaining(plus, CX) is None
    free = _account("free-1", _free_usage_exhausted(), plan="free")
    assert quota_remaining(free, CX) == 0.0
    assert free["quota_reset_at"] == RESET


def test_one_exhausted_account_among_four_does_not_reject_the_provider(tmp_path: Path) -> None:
    accounts = [
        _account("plus-1", _plus_usage(99, 100)),
        _account("plus-2", _plus_usage(70, 90)),
        _account("plus-3", _plus_usage(100, 22)),
        _account("free-1", _free_usage_exhausted(), plan="free"),
    ]
    verdicts = _verdicts(tmp_path, accounts)
    assert set(verdicts) == {CX, CODEX}
    for verdict in verdicts.values():
        assert verdict.reason != "quota_low"
        assert verdict.failed_stage is None
        # The exhausted account's reset time is not the provider's cooldown.
        assert verdict.cooldown_until is None
        # Three unknown and one exhausted: not reported as having quota either.
        assert verdict.rank_components["quota_percent"] is None


def test_every_usable_account_exhausted_rejects_until_the_earliest_reset(tmp_path: Path) -> None:
    accounts = [
        _account("free-1", _free_usage_exhausted("2026-12-08T09:45:13Z"), plan="free"),
        _account("free-2", _free_usage_exhausted("2026-12-05T00:00:00Z"), plan="free"),
        _account("free-3", _free_usage_exhausted("2026-12-20T00:00:00Z"), plan="free"),
    ]
    for verdict in _verdicts(tmp_path, accounts).values():
        assert _outcome(verdict) == ("AVAILABLE", "quota_low")
        assert verdict.cooldown_until == "2026-12-05T00:00:00+00:00"


def test_the_earliest_reset_ignores_accounts_that_are_not_usable(tmp_path: Path) -> None:
    """An inactive account and a banned one hold no quota and give no cooldown."""
    accounts = [
        _account("free-1", _free_usage_exhausted("2026-12-08T09:45:13Z"), plan="free"),
        _account(
            "free-2", _free_usage_exhausted("2026-12-01T00:00:00Z"), plan="free", isActive=False
        ),
        _account(
            "free-3",
            _free_usage_exhausted("2026-12-02T00:00:00Z"),
            plan="free",
            testStatus="banned",
        ),
    ]
    verdict = _verdicts(tmp_path, accounts)[CX]
    assert _outcome(verdict) == ("AVAILABLE", "quota_low")
    assert verdict.cooldown_until == RESET_ISO


def test_unknown_plus_exhausted_is_neither_rejected_nor_reported_as_having_quota(
    tmp_path: Path,
) -> None:
    accounts = [
        _account("plus-1"),  # no usage row at all: unknown
        _account("free-1", _free_usage_exhausted(), plan="free"),
    ]
    verdict = _verdicts(tmp_path, accounts)[CX]
    assert verdict.reason != "quota_low"
    assert verdict.failed_stage is None
    assert verdict.cooldown_until is None
    assert verdict.rank_components["quota_percent"] is None


def test_unknown_quota_is_never_healthy_and_never_exhausted(tmp_path: Path) -> None:
    only_unknown = _verdicts(tmp_path, [_account("plus-1"), _account("plus-2")])[CX]
    assert only_unknown.reason != "quota_low"
    assert only_unknown.rank_components["quota_percent"] is None
    # A measured account with quota left is the only thing that reports quota,
    # and an unknown sibling neither raises nor lowers it.
    measured = _account("plus-3", {"quotas": {"weekly": {"total": 100, "remaining": 60}}})
    mixed = _verdicts(tmp_path, [_account("plus-1"), measured])[CX]
    assert mixed.reason != "quota_low"
    assert mixed.rank_components["quota_percent"] == 60.0


@pytest.mark.parametrize(
    "unusable",
    [{"isActive": False}, {"testStatus": "banned"}, {"testStatus": "expired"}],
    ids=["inactive", "banned", "expired"],
)
def test_the_only_account_with_quota_left_is_not_usable(
    tmp_path: Path, unusable: dict[str, Any]
) -> None:
    holds_quota = _account("plus-1", {"quotas": {"weekly": {"total": 100, "remaining": 90}}})
    holds_quota.update(unusable)
    accounts = [holds_quota, _account("free-1", _free_usage_exhausted(), plan="free")]
    verdict = _verdicts(tmp_path, accounts)[CX]
    assert _outcome(verdict) == ("AVAILABLE", "quota_low")
    assert verdict.cooldown_until == RESET_ISO


def test_a_provider_with_no_usable_account_is_not_decided_by_quota() -> None:
    from verdict.orchestration.eligibility import provider_quota

    accounts = [
        _account("free-1", _free_usage_exhausted(), plan="free", isActive=False),
        _account("free-2", _free_usage_exhausted(), plan="free", testStatus="banned"),
    ]
    folded = provider_quota(accounts, "codex", CX, floor=5.0)
    assert (folded.state, folded.percent, folded.reset_at) == ("unknown", None, None)


def test_another_providers_quota_never_counts_for_this_one(tmp_path: Path) -> None:
    claude = conn("claude")
    claude["quota_percent"] = 80.0
    accounts = [claude, _account("free-1", _free_usage_exhausted(), plan="free")]
    verdict = _verdicts(tmp_path, accounts)[CX]
    assert _outcome(verdict) == ("AVAILABLE", "quota_low")


def test_a_provider_that_is_not_out_of_quota_still_fails_its_other_stages(tmp_path: Path) -> None:
    """The fold only stops one rejection; a route that fails a later stage still fails it."""
    rows = [row(CX, owned_by="codex", tools=False)]
    accounts = [_account("plus-1"), _account("free-1", _free_usage_exhausted(), plan="free")]
    ladder, _ = make_ladder(tmp_path, rows, accounts)
    verdict = ladder.evaluate(REQ, now=NOW)[0]
    assert verdict.reason != "quota_low"
    assert verdict.failed_stage is not None and verdict.failed_stage.value != "AVAILABLE"
