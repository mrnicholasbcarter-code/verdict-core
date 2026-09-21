"""S1: deterministic TaskProfile + spend-policy groups on the admit path.

Proves:
- routing test #3: free_only can never admit a paid identity (named exclusion)
- routing test #4: free_preferred keeps qualified free candidates competing,
  paid only as explicit fallback set
- profile digest is deterministic and rides on the receipt
- policy is normalized from explicit request fields, never from model names
"""

from __future__ import annotations

import pytest

from verdict.free_tier_admit import (
    REASON_SPEND_POLICY_EXCLUDES_PAID,
    REASON_WORTHY_EXCLUDES_FREE,
    FreeTierAdmitReceipt,
    NamedDrop,
    OmniRouteAdmitSnapshot,
    expand_admit_for_worthiness,
)
from verdict.task_profile import (
    SPEND_FREE_ONLY,
    SPEND_FREE_PREFERRED,
    SPEND_FRONTIER_REQUIRED,
    SPEND_PAID_ALLOWED,
    TaskProfileError,
    normalize_spend_policy,
    profile_task,
)


def _snapshot(paid_ids: tuple[str, ...]) -> OmniRouteAdmitSnapshot:
    from verdict.free_tier_admit import CatalogIdentity, ProviderConnection

    providers = tuple({ident.split("/", 1)[0] for ident in paid_ids})
    return OmniRouteAdmitSnapshot(
        catalog=tuple(
            CatalogIdentity(identity_id=ident, provider=ident.split("/", 1)[0])
            for ident in paid_ids
        ),
        free_tier=(),
        connections=tuple(ProviderConnection(provider=p, is_active=True) for p in providers),
    )


def _free_receipt() -> FreeTierAdmitReceipt:
    return FreeTierAdmitReceipt(
        admitted=("free/one", "free/two"),
        exclusions=(),
        chosen="free/one",
        empty_intersection=False,
        active_providers=("free",),
        free_tier_providers=("free",),
        free_admitted=("free/one", "free/two"),
    )


# ── spend policy normalization ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("free_only", SPEND_FREE_ONLY),
        ("FREE-ONLY", SPEND_FREE_ONLY),
        ("free_preferred", SPEND_FREE_PREFERRED),
        ("paid_allowed", SPEND_PAID_ALLOWED),
        ("frontier_required", SPEND_FRONTIER_REQUIRED),
        (None, SPEND_FREE_PREFERRED),
        ("", SPEND_FREE_PREFERRED),
    ],
)
def test_normalize_spend_policy(raw: str | None, expected: str) -> None:
    assert normalize_spend_policy(raw) == expected


def test_unknown_spend_policy_is_refused_not_guessed() -> None:
    with pytest.raises(TaskProfileError):
        normalize_spend_policy("cheapest_possible")


def test_non_string_spend_policy_is_refused_not_defaulted() -> None:
    with pytest.raises(TaskProfileError, match="spend_policy must be a string"):
        profile_task("repair x", context={"spend_policy": 123})


# ── profile determinism ─────────────────────────────────────────────────────


def test_profile_digest_is_deterministic() -> None:
    a = profile_task("repair the parser bug", context={"spend_policy": "free_only"})
    b = profile_task("repair the parser bug", context={"spend_policy": "free_only"})
    assert a.digest == b.digest
    assert a.digest.startswith("sha256:")
    assert a.spend_policy == SPEND_FREE_ONLY


def test_profile_digest_changes_with_policy_and_task() -> None:
    base = profile_task("repair the parser bug")
    assert (
        profile_task("repair the parser bug", context={"spend_policy": "free_only"}).digest
        != base.digest
    )
    assert profile_task("refactor the whole module tree").digest != base.digest


def test_profile_preserves_task_class_hint_and_requirements() -> None:
    profile = profile_task(
        "add vision parsing", context={"vision_required": True, "task_class": "worthy"}
    )
    assert "vision" in profile.required_capabilities
    assert profile.task_class_hint == "worthy"


# ── policy enforcement in admission ─────────────────────────────────────────


def test_free_only_never_admits_paid_and_names_the_exclusion() -> None:
    snapshot = _snapshot(("paid/strong", "paid/frontier-x"))
    receipt = expand_admit_for_worthiness(
        _free_receipt(),
        snapshot,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_ONLY,
    )

    assert receipt.admitted == ("free/one", "free/two")
    assert receipt.paid_admitted == ()
    excluded = {drop.model_id: drop.reason for drop in receipt.exclusions}
    assert excluded["paid/strong"] == REASON_SPEND_POLICY_EXCLUDES_PAID
    assert excluded["paid/frontier-x"] == REASON_SPEND_POLICY_EXCLUDES_PAID


def test_free_only_never_admits_paid_for_worthy_tasks() -> None:
    snapshot = _snapshot(("paid/frontier-a", "paid/strong"))
    receipt = expand_admit_for_worthiness(
        _free_receipt(),
        snapshot,
        task_class="worthy",
        class_reasons=("server-classified-worthy",),
        spend_policy=SPEND_FREE_ONLY,
    )

    assert receipt.admitted == ()
    assert receipt.chosen is None
    assert receipt.paid_admitted == ()
    assert {drop.reason for drop in receipt.exclusions} >= {
        REASON_WORTHY_EXCLUDES_FREE,
        REASON_SPEND_POLICY_EXCLUDES_PAID,
    }


def test_free_preferred_keeps_free_competing_and_marks_paid_fallback() -> None:
    snapshot = _snapshot(("paid/strong",))
    receipt = expand_admit_for_worthiness(
        _free_receipt(),
        snapshot,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=SPEND_FREE_PREFERRED,
    )

    # Free candidates stay admitted and chosen remains free when free qualifies.
    assert "free/one" in receipt.admitted and "free/two" in receipt.admitted
    assert receipt.chosen == "free/one"
    # Paid may exist only as an explicit fallback set, never silently chosen.
    assert receipt.paid_admitted == ("paid/strong",)
    assert "paid/strong" in receipt.admitted


def test_frontier_required_drops_free_for_frontier_only() -> None:
    snapshot = _snapshot(("paid/frontier-a", "paid/lesser"))
    receipt = expand_admit_for_worthiness(
        _free_receipt(),
        snapshot,
        task_class="worthy",
        class_reasons=("test",),
        spend_policy=SPEND_FRONTIER_REQUIRED,
        frontier_allowlist=("paid/frontier-a",),
    )

    assert receipt.admitted == ("paid/frontier-a",)
    assert receipt.chosen == "paid/frontier-a"


def test_receipt_carries_profile_digest_and_policy() -> None:
    profile = profile_task("repair x", context={"spend_policy": "free_only"})
    snapshot = _snapshot(("paid/strong",))
    receipt = expand_admit_for_worthiness(
        _free_receipt(),
        snapshot,
        task_class="ordinary",
        class_reasons=(),
        spend_policy=profile.spend_policy,
        task_profile_digest=profile.digest,
    )
    payload = receipt.to_dict()

    assert payload["task_profile_digest"] == profile.digest
    assert payload["spend_policy"] == SPEND_FREE_ONLY
    assert any(
        drop.model_id == "paid/strong" and drop.reason == REASON_SPEND_POLICY_EXCLUDES_PAID
        for drop in receipt.exclusions
    )


def test_named_drop_shape_unchanged() -> None:
    drop = NamedDrop("m", "r", "d")
    assert drop.to_dict() == {"model": "m", "reason": "r", "detail": "d"}
