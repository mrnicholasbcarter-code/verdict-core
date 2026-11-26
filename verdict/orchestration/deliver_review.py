"""Production cross-family reviewer for the ``verdict deliver`` ready gate.

``make_cross_family_reviewer`` returns a :data:`ready_gate.CrossFamilyReviewer`.
It reuses the existing :class:`OpenCodeReviewer` invocation (built the way
``verdict orchestrate`` builds it) on ``base_branch`` .. ``head_sha`` with every
family in the request's ``exclude_families`` excluded from the reviewer routes.

It fails closed: when no reviewer route of another family is admissible it
raises :class:`ready_gate.NoCrossFamilyReviewerError`, and a skipped, empty,
unidentified or otherwise not-clean review comes back as an ``ERROR`` result,
never as a pass. The route source and the reviewer are injectable so tests run
no process, network or model call. See the ``verdict-deliver`` OpenSpec change.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from verdict.orchestration.contracts import ModelSelector, Reviewer, ReviewResult, route_family
from verdict.orchestration.ready_gate import (
    CrossFamilyReviewer,
    CrossFamilyReviewRequest,
    NoCrossFamilyReviewerError,
)
from verdict.orchestration.review import OpenCodeReviewer

DEFAULT_GATEWAY = "http://127.0.0.1:20128"
_NO_REVIEWER_MARKER = "no independent reviewer"

SelectorSource = Callable[[], ModelSelector]
ReviewerFactory = Callable[[ModelSelector], Reviewer]


def _gateway_selector_source(gateway: str) -> SelectorSource:
    def source() -> ModelSelector:
        from verdict.credentials_store import resolve_api_key
        from verdict.orchestration.eligibility_report import build_selector

        if resolve_api_key() is None:
            raise RuntimeError("set VERDICT_OMNIROUTE_API_KEY for the OmniRoute gateway")
        selector: ModelSelector = build_selector(gateway, scope="", prefer="")
        return selector

    return source


def _open_code_reviewer_factory(gateway: str, out_dir: Path) -> ReviewerFactory:
    def factory(selector: ModelSelector) -> Reviewer:
        return OpenCodeReviewer(selector, out_dir=out_dir, gateway_url=gateway.rstrip("/") + "/v1")

    return factory


def _error(detail: str, result: ReviewResult) -> ReviewResult:
    return replace(result, status="ERROR", detail=detail)


def _checked(result: ReviewResult, excluded: frozenset[str]) -> ReviewResult:
    """Return ``result`` unchanged only when it is a real, clean, cross-family review."""
    if result.status == "ERROR" and _NO_REVIEWER_MARKER in result.detail:
        raise NoCrossFamilyReviewerError(result.detail)
    if result.status == "PASS":
        if not result.route_id:
            return _error("review passed without naming the reviewer route", result)
        if route_family(result.route_id) in excluded:
            raise NoCrossFamilyReviewerError(
                f"reviewer route {result.route_id!r} is in an excluded family"
            )
        if not result.passed:
            return _error("review reported PASS with blocking findings or lost findings", result)
    return result


def make_cross_family_reviewer(
    *,
    repo: Path,
    out_dir: Path,
    gateway: str = DEFAULT_GATEWAY,
    selector_source: SelectorSource | None = None,
    reviewer_factory: ReviewerFactory | None = None,
) -> CrossFamilyReviewer:
    """Build the production reviewer the ready gate calls for a route-level run.

    ``selector_source`` supplies the reviewer routes (production: the live
    OmniRoute eligibility ladder) and ``reviewer_factory`` builds the review
    runner over that selector (production: :class:`OpenCodeReviewer`). Both are
    called lazily, only when the gate actually asks for a review.
    """
    source = selector_source or _gateway_selector_source(gateway)
    factory = reviewer_factory or _open_code_reviewer_factory(gateway, Path(out_dir))

    def review(request: CrossFamilyReviewRequest) -> ReviewResult:
        excluded = frozenset(f for f in request.exclude_families if f)
        if not excluded:
            raise NoCrossFamilyReviewerError(
                "no implementer family is recorded, so no reviewer can be shown to be another family"
            )
        runner = factory(source())
        result = asyncio.run(
            runner.review(
                repo=Path(repo),
                base_ref=request.base_branch,
                head_ref=request.head_sha,
                background=(
                    f"Independent cross-family review of {request.pr_ref} "
                    f"({request.base_branch}...{request.head_sha})"
                ),
                exclude_routes=frozenset(),
                exclude_families=excluded,
            )
        )
        return _checked(result, excluded)

    return review
