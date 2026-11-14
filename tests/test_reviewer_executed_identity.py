"""Observed reviewer identity is required for a pass and for independence.

OCR ``manifest.execution.model`` is what OCR reports, not a verified
gateway-served model. A missing, blank, or unusable report is not a pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_orch_review import (
    FakeSelector,
    OcrRun,
    PayloadRunner,
    PoolSelector,
    _reviewer,
    _run,
    _verdict,
)

_POSITIVE_COVERAGE = {"manifest": {"coverage": {"selected": [{"path": "x.py"}]}}, "findings": []}


def _covered(model: object | None = None, *, include_model: bool = True) -> dict[str, object]:
    manifest: dict[str, object] = {"coverage": {"selected": [{"path": "x.py"}]}}
    if include_model:
        manifest["execution"] = {"model": model}
    return {"manifest": manifest, "findings": []}


def _interpret_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]):
    """Call ``_interpret`` on the payload as written, with no runner rewrite."""
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    raw = tmp_path / "ocr-raw.json"
    raw.write_text(json.dumps(payload))
    reviewer = _reviewer(tmp_path, FakeSelector(_verdict("cx/gpt-5.5-low")), PayloadRunner(["{}"]))
    return reviewer._interpret(OcrRun(exit_code=0), raw, "cx/gpt-5.5-low")


def test_positive_coverage_without_observed_model_is_error_not_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A covered payload with no OCR-reported model is not a clean pass."""
    result = _interpret_payload(tmp_path, monkeypatch, _POSITIVE_COVERAGE)
    assert result.status == "ERROR"
    assert not result.passed
    assert result.observed_model == ""
    assert result.detail.startswith("identity_unobserved:")
    assert "cx/gpt-5.5-low" in result.detail


def test_blank_observed_model_is_unobserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _interpret_payload(tmp_path, monkeypatch, _covered("   "))
    assert result.status == "ERROR"
    assert result.detail.startswith("identity_unobserved:")


def test_non_string_observed_model_is_not_stringified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _interpret_payload(tmp_path, monkeypatch, _covered({"id": "cx/gpt-5.5-low"}))
    assert result.status == "ERROR"
    assert result.detail.startswith("identity_unobserved:")
    assert "identity_mismatch" not in result.detail


def test_opaque_observed_model_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _interpret_payload(tmp_path, monkeypatch, _covered("auto/coding"))
    assert result.status == "ERROR"
    assert result.detail.startswith("identity_unobserved:")
    prefixed = _interpret_payload(tmp_path, monkeypatch, _covered("omniroute/auto/coding"))
    assert prefixed.status == "ERROR"
    assert prefixed.detail.startswith("identity_unobserved:")


def test_unobserved_identity_reselects_another_independent_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """identity_unobserved is reviewer-scoped: try another reviewer, same budget."""
    monkeypatch.setenv("TEST_OCR_KEY", "k")
    missing = _POSITIVE_COVERAGE
    good = _covered("cx/b")
    selector = PoolSelector(["cc/a", "cx/b"])
    runner = PayloadRunner([json.dumps(missing), json.dumps(good)])
    result = _run(_reviewer(tmp_path, selector, runner))
    assert result.status == "PASS"
    assert result.route_id == "cx/b"
    assert result.observed_model == "cx/b"
    assert [attempt["route_id"] for attempt in result.attempts] == ["cc/a", "cx/b"]
    assert result.attempts[0]["category"] == "model_mismatch"
    assert result.attempts[0]["scope"] == "route"
    assert "identity_unobserved" in str(result.attempts[0]["detail"])
    assert selector.failed == ["cc/a"]
    launched = [
        c["argv"][c["argv"].index("--model") + 1] for c in runner.calls if "--model" in c["argv"]
    ]
    assert launched == ["cc/a", "cx/b"]


class _Missing:
    pass


_MISSING = _Missing()


def _event(seq: int, type_: str, data: dict[str, object], node_id: str = "n") -> dict[str, object]:
    return {
        "seq": seq,
        "at": "2026-09-28T00:00:00Z",
        "type": type_,
        "node_id": node_id,
        "data": data,
    }


def _independent_events(
    *,
    reviewer_route: str = "cx/gpt-5.5",
    observed: object = _MISSING,
    worker_route: str = "kr/claude-sonnet-4",
    worker_executed: str = "kr/claude-sonnet-4",
) -> list[dict[str, object]]:
    review: dict[str, object] = {"status": "PASS", "route_id": reviewer_route, "blocking": 0}
    if observed is not _MISSING:
        review["observed_model"] = observed
    return [
        _event(1, "dispatch", {"route_id": worker_route, "attempt": 1}),
        _event(2, "terminal", {"attempt": 1, "ok": True, "reported_model": worker_executed}),
        _event(90, "review", review, ""),
    ]


def test_claim_missing_observed_model_is_not_observed() -> None:
    from verdict.orchestration.claims import CLAIM_STATUS_NOT_OBSERVED, derive_claims

    claim = next(
        c for c in derive_claims(_independent_events(), {}) if c.id == "independent_review"
    )
    assert claim.status == CLAIM_STATUS_NOT_OBSERVED


def test_legacy_stream_without_observed_model_key_is_not_observed() -> None:
    """Old events are not grandfathered into VERIFIED. Receipt bytes stay untouched."""
    from verdict.orchestration.claims import CLAIM_STATUS_NOT_OBSERVED, derive_claims

    events = _independent_events()
    assert "observed_model" not in events[-1]["data"]
    claim = next(c for c in derive_claims(events, {}) if c.id == "independent_review")
    assert claim.status == CLAIM_STATUS_NOT_OBSERVED


def test_claim_blank_and_unusable_observed_model_is_not_observed() -> None:
    from verdict.orchestration.claims import CLAIM_STATUS_NOT_OBSERVED, derive_claims

    for observed in (
        "",
        "   ",
        {"id": "cx/gpt-5.5"},
        ["cx/gpt-5.5"],
        "auto/coding",
        "omniroute/auto/coding",
    ):
        claim = next(
            c
            for c in derive_claims(_independent_events(observed=observed), {})
            if c.id == "independent_review"
        )
        assert claim.status == CLAIM_STATUS_NOT_OBSERVED, observed


def test_claim_observed_worker_identity_is_contradicted_even_if_selected_route_differs() -> None:
    from verdict.orchestration.claims import CLAIM_STATUS_CONTRADICTED, derive_claims

    events = _independent_events(
        reviewer_route="cx/gpt-5.5",
        observed="kr/claude-sonnet-4",
        worker_executed="kr/claude-sonnet-4",
    )
    claim = next(c for c in derive_claims(events, {}) if c.id == "independent_review")
    assert claim.status == CLAIM_STATUS_CONTRADICTED
    assert "kr/claude-sonnet-4" in claim.evidence[0].value["shared_identities"]


def test_claim_observed_shared_family_or_aliased_pool_is_not_observed() -> None:
    from verdict.orchestration.claims import CLAIM_STATUS_NOT_OBSERVED, derive_claims

    family = _independent_events(
        reviewer_route="cx/gpt-5.5",
        observed="agy/claude-opus-4",
        worker_executed="kr/claude-sonnet-4",
    )
    family_claim = next(c for c in derive_claims(family, {}) if c.id == "independent_review")
    assert family_claim.status == CLAIM_STATUS_NOT_OBSERVED
    assert "claude" in family_claim.evidence[0].value["shared_families"]

    pool = _independent_events(
        reviewer_route="cx/gpt-5.5",
        observed="antigravity/gpt-oss-120b-medium",
        worker_route="agy/claude-sonnet-4-6",
        worker_executed="agy/claude-sonnet-4-6",
    )
    pool_claim = next(c for c in derive_claims(pool, {}) if c.id == "independent_review")
    assert pool_claim.status == CLAIM_STATUS_NOT_OBSERVED
    assert "google-antigravity" in pool_claim.evidence[0].value["shared_pools"]


def test_claim_genuinely_independent_observed_reviewer_is_verified() -> None:
    from verdict.orchestration.claims import CLAIM_STATUS_VERIFIED, derive_claims

    events = _independent_events(reviewer_route="cx/gpt-5.5", observed="cx/gpt-5.5")
    claim = next(c for c in derive_claims(events, {}) if c.id == "independent_review")
    assert claim.status == CLAIM_STATUS_VERIFIED
    assert "cx/gpt-5.5" in claim.evidence[0].value["reviewer_routes"]
    assert claim.evidence[0].value["shared_identities"] == []
    assert claim.evidence[0].value["shared_families"] == []


def test_claim_observed_identity_is_checked_not_only_the_selected_route() -> None:
    """A review_attempt route that shares a family is also not independent."""
    from verdict.orchestration.claims import CLAIM_STATUS_NOT_OBSERVED, derive_claims

    events = _independent_events(reviewer_route="cx/gpt-5.5", observed="cx/gpt-5.5")
    events.insert(2, _event(80, "review_attempt", {"route_id": "kr/claude-haiku"}, ""))
    claim = next(c for c in derive_claims(events, {}) if c.id == "independent_review")
    assert claim.status == CLAIM_STATUS_NOT_OBSERVED
    assert "claude" in claim.evidence[0].value["shared_families"]
