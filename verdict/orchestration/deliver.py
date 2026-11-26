"""Delivery contracts and the fail-closed eligibility gate for ``verdict deliver``.

This module is pure: it reads a finished run directory and decides whether the
run may be delivered as a pull request. It never runs git, ``gh`` or any other
subprocess and never touches the network. It writes nothing, so
``events.jsonl`` and ``receipt.json`` are left byte-identical.

``plan_delivery`` returns a ``DeliverPlan`` for a verified ``COMPLETE`` run, or
a ``DeliverRefusal`` carrying a stable ``DeliverReason`` code. See the
``verdict-deliver`` OpenSpec change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    NodeKind,
    OrchestrationError,
    RunEvent,
    RunOutcome,
    route_family,
)
from verdict.orchestration.receipt import EVENTS_FILE, RECEIPT_FILE, EventLog, verify_run_receipt

DEFAULT_BASE_BRANCH = "main"
BRANCH_PREFIX = "verdict/issue-"
MAX_SLUG_LENGTH = 40
_REVIEW_PASS = "PASS"
_REVIEW_INDEPENDENCE_STATE = "REVIEW_INDEPENDENCE"
_SLUG_DROP = re.compile(r"[^a-z0-9-]")


class DeliverReason(str, Enum):
    """Stable refusal codes. Values are part of the delivery record contract."""

    INVALID_REQUEST = "invalid_request"
    RUN_NOT_FOUND = "run_not_found"
    RUN_IN_PROGRESS = "run_in_progress"
    RUN_NOT_COMPLETE = "run_not_complete"
    RECEIPT_MISSING = "receipt_missing"
    RECEIPT_INVALID = "receipt_invalid"
    REVIEW_FAILED = "review_failed"
    NOTHING_TO_DELIVER = "nothing_to_deliver"


@dataclass(frozen=True)
class DeliverRequest:
    """What the operator asked for. ``issue_title`` is untrusted and only feeds the slug."""

    run_dir: Path
    issue_number: int
    issue_title: str = ""
    base_branch: str = DEFAULT_BASE_BRANCH


@dataclass(frozen=True)
class VerificationResult:
    command: str
    exit_code: int | None


@dataclass(frozen=True)
class NodeSummary:
    node_id: str
    kind: str
    final_state: str
    commit: str | None
    routes: tuple[str, ...]
    verification: tuple[VerificationResult, ...]


@dataclass(frozen=True)
class ReviewEvidence:
    status: str
    reviewer: str
    route_id: str
    blocking: int
    # "family", "route" or "unknown" (no REVIEW_INDEPENDENCE event recorded).
    level: str
    implementer_families: tuple[str, ...]
    reviewer_family: str


@dataclass(frozen=True)
class DeliverPlan:
    run_id: str
    goal: str
    issue_number: int
    base_branch: str
    head_branch: str
    integration_commit: str
    receipt_path: Path
    receipt_digest: str
    events_digest: str
    openspec_change: str | None
    nodes: tuple[NodeSummary, ...]
    review: ReviewEvidence


@dataclass(frozen=True)
class DeliverRefusal:
    code: DeliverReason
    message: str
    run_dir: Path | None = None


DeliverDecision = DeliverPlan | DeliverRefusal


def slugify_title(title: str) -> str:
    """Lower-case ``[a-z0-9-]``, at most 40 characters; every other character is dropped."""
    kept = _SLUG_DROP.sub("", title.lower())
    return kept[:MAX_SLUG_LENGTH].strip("-")


def head_branch_name(issue_number: int, issue_title: str = "") -> str:
    """Deterministic ``verdict/issue-<N>-<slug>`` (``verdict/issue-<N>`` for an empty slug)."""
    slug = slugify_title(issue_title)
    return f"{BRANCH_PREFIX}{issue_number}-{slug}" if slug else f"{BRANCH_PREFIX}{issue_number}"


def _refuse(code: DeliverReason, message: str, run_dir: Path | None) -> DeliverRefusal:
    return DeliverRefusal(code=code, message=message, run_dir=run_dir)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integration_commit(events: list[RunEvent]) -> str:
    finished = [e for e in events if e.type == "run_finished"]
    if finished:
        ref = finished[-1].data.get("integration_ref")
        if isinstance(ref, str) and ref.strip():
            return ref.strip()
    for event in reversed(events):
        if event.type == "integrate":
            commit = event.data.get("commit")
            if isinstance(commit, str) and commit.strip():
                return commit.strip()
    return ""


def _review_level(receipt: Mapping[str, Any]) -> str:
    level = "unknown"
    for event in receipt.get("controller_events") or []:
        data = _as_mapping(event)
        if data.get("state") == _REVIEW_INDEPENDENCE_STATE and data.get("level") in {
            "family",
            "route",
        }:
            level = str(data["level"])
    return level


def _command_text(command: Any) -> str:
    if isinstance(command, list | tuple):
        return " ".join(str(part) for part in command)
    return str(command or "")


def _node_summary(node: Mapping[str, Any]) -> NodeSummary:
    routes: list[str] = []
    for attempt in node.get("attempts") or []:
        route = str(_as_mapping(attempt).get("route_id") or "")
        if route and route not in routes:
            routes.append(route)
    verification = tuple(
        VerificationResult(
            command=_command_text(_as_mapping(v).get("command")),
            exit_code=(
                code
                if isinstance(code := _as_mapping(v).get("exit_code"), int)
                and not isinstance(code, bool)
                else None
            ),
        )
        for v in node.get("validated_by") or []
    )
    commit = node.get("commit")
    return NodeSummary(
        node_id=str(node.get("node_id") or ""),
        kind=str(node.get("kind") or ""),
        final_state=str(node.get("final_state") or ""),
        commit=str(commit) if commit else None,
        routes=tuple(routes),
        verification=verification,
    )


def plan_delivery(request: DeliverRequest) -> DeliverDecision:
    """Decide whether ``request.run_dir`` may be delivered. Pure: reads files only."""
    run_dir = Path(request.run_dir)

    issue = request.issue_number
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        return _refuse(
            DeliverReason.INVALID_REQUEST,
            f"issue number must be a positive integer, got {issue!r}",
            run_dir,
        )
    if not request.base_branch.strip():
        return _refuse(DeliverReason.INVALID_REQUEST, "base branch must not be empty", run_dir)

    events_path = run_dir / EVENTS_FILE
    if not run_dir.is_dir() or not events_path.is_file():
        return _refuse(
            DeliverReason.RUN_NOT_FOUND, f"no run found at {run_dir} (missing {EVENTS_FILE})", None
        )

    try:
        events = EventLog(events_path, repair=False).read()
    except (OrchestrationError, OSError, ValueError) as exc:
        return _refuse(
            DeliverReason.RECEIPT_INVALID,
            f"cannot read {EVENTS_FILE} for {run_dir.name}: {exc}",
            run_dir,
        )

    finished = [e for e in events if e.type == "run_finished"]
    if not finished:
        return _refuse(
            DeliverReason.RUN_IN_PROGRESS,
            f"run {run_dir.name} has not finished (no run_finished event)",
            run_dir,
        )
    claimed = str(finished[-1].data.get("outcome") or "")
    if claimed != RunOutcome.COMPLETE.value:
        return _refuse(
            DeliverReason.RUN_NOT_COMPLETE,
            f"run {run_dir.name} ended {claimed or 'without an outcome'}, not COMPLETE",
            run_dir,
        )

    receipt_path = run_dir / RECEIPT_FILE
    if not receipt_path.is_file():
        return _refuse(
            DeliverReason.RECEIPT_MISSING, f"run {run_dir.name} has no {RECEIPT_FILE}", run_dir
        )
    try:
        problems = verify_run_receipt(run_dir, repair=False)
        receipt_bytes = receipt_path.read_bytes()
    except (OrchestrationError, OSError, ValueError) as exc:
        problems = [str(exc)]
        receipt_bytes = b""
    if problems:
        return _refuse(
            DeliverReason.RECEIPT_INVALID,
            f"receipt for run {run_dir.name} does not verify: {'; '.join(problems)}",
            run_dir,
        )
    try:
        parsed: Any = json.loads(receipt_bytes)
    except ValueError as exc:
        return _refuse(
            DeliverReason.RECEIPT_INVALID, f"cannot parse {RECEIPT_FILE}: {exc}", run_dir
        )
    receipt = _as_mapping(parsed)

    review = _as_mapping(receipt.get("review"))
    review_status = str(review.get("status") or "MISSING")
    blocking_raw = review.get("blocking")
    blocking = (
        blocking_raw if isinstance(blocking_raw, int) and not isinstance(blocking_raw, bool) else 0
    )
    if review_status != _REVIEW_PASS or blocking:
        return _refuse(
            DeliverReason.REVIEW_FAILED,
            f"review gate not passed for run {run_dir.name}: status {review_status}, "
            f"{blocking} blocking finding(s)",
            run_dir,
        )

    outcome = str(receipt.get("outcome") or "")
    if outcome != RunOutcome.COMPLETE.value:
        return _refuse(
            DeliverReason.RUN_NOT_COMPLETE,
            f"receipt for run {run_dir.name} records outcome {outcome or 'none'}: "
            f"{receipt.get('reason', '')}",
            run_dir,
        )

    nodes = tuple(_node_summary(_as_mapping(n)) for n in receipt.get("nodes") or [])
    integration = _integration_commit(events)
    implement_ids = {n.node_id for n in nodes if n.kind == NodeKind.IMPLEMENT.value}
    unchanged = {str(n) for n in receipt.get("no_change_nodes") or []}
    if not integration:
        return _refuse(
            DeliverReason.NOTHING_TO_DELIVER,
            f"run {run_dir.name} recorded no integration commit",
            run_dir,
        )
    if implement_ids and implement_ids <= unchanged:
        return _refuse(
            DeliverReason.NOTHING_TO_DELIVER,
            f"run {run_dir.name} changed nothing: every implement node reported no change",
            run_dir,
        )

    implementer_routes = [r for n in nodes if n.kind == NodeKind.IMPLEMENT.value for r in n.routes]
    reviewer_route = str(review.get("route_id") or "")
    openspec = _as_mapping(receipt.get("openspec"))
    change = openspec.get("openspec_change")
    run_id = str(receipt.get("run_id") or run_dir.name)
    return DeliverPlan(
        run_id=run_id,
        goal=str(receipt.get("goal") or ""),
        issue_number=issue,
        base_branch=request.base_branch.strip(),
        head_branch=head_branch_name(issue, request.issue_title),
        integration_commit=integration,
        receipt_path=receipt_path,
        receipt_digest="sha256:" + hashlib.sha256(receipt_bytes).hexdigest(),
        events_digest=str(receipt.get("events_digest") or ""),
        openspec_change=str(change) if change else None,
        nodes=nodes,
        review=ReviewEvidence(
            status=review_status,
            reviewer=str(review.get("reviewer") or ""),
            route_id=reviewer_route,
            blocking=blocking,
            level=_review_level(receipt),
            implementer_families=tuple(sorted({route_family(r) for r in implementer_routes})),
            reviewer_family=route_family(reviewer_route) if reviewer_route else "",
        ),
    )
