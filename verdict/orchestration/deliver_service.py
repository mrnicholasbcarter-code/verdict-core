"""Single entry point for ``verdict deliver``: gate, render, publish, record.

``deliver_run`` composes the pure eligibility gate (``deliver``), the pure
renderer (``deliver_render``) and the git/``gh`` adapter (``deliver_publish``):

1. a refused run returns a ``REFUSED`` outcome and never reaches the publisher;
2. a run already delivered (a valid ``delivery.json`` that matches this run's id,
   integration commit and digests) returns the recorded PR without any command, so a
   repeated deliver is idempotent; a record for anything else is
   ``delivery_record_mismatch``;
3. ``dry_run`` returns the plan, title and body and runs no command at all;
4. otherwise the publisher runs, and only on success is the delivery record
   (``delivery.json`` plus ``delivery.sha256``) written next to the run's durable
   state. ``events.jsonl`` and ``receipt.json`` are never written.

See the ``verdict-deliver`` OpenSpec change.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.orchestration.deliver import (
    DeliverPlan,
    DeliverReason,
    DeliverRefusal,
    DeliverRequest,
    plan_delivery,
)
from verdict.orchestration.deliver_publish import (
    DEFAULT_PUSH_TIMEOUT_S,
    CommandRunner,
    PublishError,
    run_command,
)
from verdict.orchestration.deliver_publish import publish_branch_and_pr as _publish
from verdict.orchestration.deliver_render import render_pull_request
from verdict.orchestration.ready_gate import (
    DEFAULT_CI_DEADLINE_S,
    CrossFamilyReviewer,
    run_pr_ready_gate,
)

DELIVERY_FILE = "delivery.json"
DELIVERY_DIGEST_FILE = "delivery.sha256"
DELIVERY_SCHEMA = "verdict.delivery/1"
READY_SKIPPED = "skipped"

Clock = Callable[[], datetime]


class DeliverStatus(str, Enum):
    DELIVERED = "delivered"
    ALREADY_DELIVERED = "already_delivered"
    DRY_RUN = "dry_run"
    REFUSED = "refused"
    FAILED = "failed"


class DeliverServiceReason(str, Enum):
    """Failure codes owned by the service (gate and publisher codes keep their own)."""

    DELIVERY_RECORD_INVALID = "delivery_record_invalid"
    DELIVERY_RECORD_MISMATCH = "delivery_record_mismatch"
    RECORD_WRITE_FAILED = "record_write_failed"


@dataclass(frozen=True)
class DeliverRunRequest(DeliverRequest):
    """A ``DeliverRequest`` plus how to run it."""

    dry_run: bool = False
    repo_dir: Path | None = None
    ci_deadline_s: float = DEFAULT_CI_DEADLINE_S
    run_ready_gate: bool = True
    push_timeout_s: float = DEFAULT_PUSH_TIMEOUT_S


@dataclass(frozen=True)
class DeliveryRecord:
    run_id: str
    issue_number: int
    pr_url: str
    branch: str
    base_branch: str
    integration_commit: str
    receipt_digest: str
    events_digest: str
    delivered_at: str
    created: bool
    ready: bool = False
    ready_reason: str = ""
    review_level: str = ""
    reviewer_route: str = ""
    extra_review_ran: bool = False
    ci_polls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": DELIVERY_SCHEMA,
            "run_id": self.run_id,
            "issue_number": self.issue_number,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "integration_commit": self.integration_commit,
            "receipt_digest": self.receipt_digest,
            "events_digest": self.events_digest,
            "delivered_at": self.delivered_at,
            "created": self.created,
            "ready": self.ready,
            "ready_reason": self.ready_reason,
            "review_level": self.review_level,
            "reviewer_route": self.reviewer_route,
            "extra_review_ran": self.extra_review_ran,
            "ci_polls": self.ci_polls,
        }


@dataclass(frozen=True)
class DeliverOutcome:
    status: DeliverStatus
    reason: str = ""
    message: str = ""
    run_id: str = ""
    pr_url: str = ""
    branch: str = ""
    base_branch: str = ""
    integration_commit: str = ""
    title: str = ""
    body: str = ""
    delivered_at: str = ""
    created: bool = False
    record_path: Path | None = None
    ready: bool = False
    ready_reason: str = ""
    review_level: str = ""
    reviewer_route: str = ""
    extra_review_ran: bool = False
    ci_polls: int = 0

    @property
    def ok(self) -> bool:
        return self.status in {
            DeliverStatus.DELIVERED,
            DeliverStatus.ALREADY_DELIVERED,
            DeliverStatus.DRY_RUN,
        }

    def to_dict(self) -> dict[str, Any]:
        """JSON-serializable form for the CLI."""
        return {
            "status": self.status.value,
            "ok": self.ok,
            "reason": self.reason,
            "message": self.message,
            "run_id": self.run_id,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "base_branch": self.base_branch,
            "integration_commit": self.integration_commit,
            "title": self.title,
            "body": self.body,
            "delivered_at": self.delivered_at,
            "created": self.created,
            "record_path": str(self.record_path) if self.record_path is not None else None,
            "ready": self.ready,
            "ready_reason": self.ready_reason,
            "review_level": self.review_level,
            "reviewer_route": self.reviewer_route,
            "extra_review_ran": self.extra_review_ran,
            "ci_polls": self.ci_polls,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(clock: Clock) -> str:
    moment = clock()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, data: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_delivery_record(run_dir: Path, record: DeliveryRecord) -> Path:
    """Write ``delivery.sha256`` then ``delivery.json`` atomically; return the json path.

    The digest goes first so a crash in between leaves no ``delivery.json``, which
    reads as "not delivered" rather than as a corrupt record.
    """
    data = (json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(run_dir / DELIVERY_DIGEST_FILE, (_digest(data) + "\n").encode("utf-8"))
    path = run_dir / DELIVERY_FILE
    _atomic_write(path, data)
    return path


def read_delivery_record(run_dir: Path) -> DeliveryRecord | None:
    """Return the verified record, ``None`` when absent. Raises ``ValueError`` if invalid."""
    path = run_dir / DELIVERY_FILE
    if not path.exists():
        return None
    try:
        data = path.read_bytes()
        expected = (run_dir / DELIVERY_DIGEST_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"cannot read delivery record: {exc}") from exc
    if expected != _digest(data):
        raise ValueError(f"{DELIVERY_FILE} does not match {DELIVERY_DIGEST_FILE}")
    try:
        raw: Any = json.loads(data)
    except ValueError as exc:
        raise ValueError(f"{DELIVERY_FILE} is not valid JSON: {exc}") from exc
    if not isinstance(raw, Mapping) or raw.get("schema") != DELIVERY_SCHEMA:
        raise ValueError(f"{DELIVERY_FILE} has an unknown schema")
    issue = raw.get("issue_number")
    url = raw.get("pr_url")
    if isinstance(issue, bool) or not isinstance(issue, int) or not isinstance(url, str) or not url:
        raise ValueError(f"{DELIVERY_FILE} is missing the PR url or issue number")
    return DeliveryRecord(
        run_id=str(raw.get("run_id") or ""),
        issue_number=issue,
        pr_url=url,
        branch=str(raw.get("branch") or ""),
        base_branch=str(raw.get("base_branch") or ""),
        integration_commit=str(raw.get("integration_commit") or ""),
        receipt_digest=str(raw.get("receipt_digest") or ""),
        events_digest=str(raw.get("events_digest") or ""),
        delivered_at=str(raw.get("delivered_at") or ""),
        created=bool(raw.get("created")),
        ready=raw.get("ready") is True,
        ready_reason=str(raw.get("ready_reason") or ""),
        review_level=str(raw.get("review_level") or ""),
        reviewer_route=str(raw.get("reviewer_route") or ""),
        extra_review_ran=raw.get("extra_review_ran") is True,
        ci_polls=_count(raw.get("ci_polls")),
    )


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _outcome(plan: DeliverPlan, status: DeliverStatus, **fields: Any) -> DeliverOutcome:
    """Build an outcome carrying the plan's identity; ``fields`` override and extend it."""
    base: dict[str, Any] = {
        "run_id": plan.run_id,
        "branch": plan.head_branch,
        "base_branch": plan.base_branch,
        "integration_commit": plan.integration_commit,
    }
    return DeliverOutcome(status=status, **{**base, **fields})


def _refused(refusal: DeliverRefusal) -> DeliverOutcome:
    return DeliverOutcome(
        status=DeliverStatus.REFUSED, reason=refusal.code.value, message=refusal.message
    )


def _record_mismatches(record: DeliveryRecord, plan: DeliverPlan) -> list[str]:
    """Names of the identity fields where a stored record differs from the current run.

    A record is only reusable for the very run and commit it was written for: the issue
    number alone would let a valid record copied from another run suppress publication.
    """
    pairs: tuple[tuple[str, object, object], ...] = (
        ("issue_number", record.issue_number, plan.issue_number),
        ("run_id", record.run_id, plan.run_id),
        ("integration_commit", record.integration_commit, plan.integration_commit),
        ("receipt_digest", record.receipt_digest, plan.receipt_digest),
        ("events_digest", record.events_digest, plan.events_digest),
    )
    return [name for name, stored, current in pairs if stored != current]


def _push_timeout_s(request: DeliverRunRequest | DeliverRequest) -> float | None:
    """The request's push timeout in seconds; ``None`` when it is not finite and positive."""
    try:
        seconds = float(getattr(request, "push_timeout_s", DEFAULT_PUSH_TIMEOUT_S))
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def deliver_run(
    request: DeliverRunRequest | DeliverRequest,
    *,
    runner: CommandRunner | None = None,
    clock: Clock | None = None,
    reviewer: CrossFamilyReviewer | None = None,
) -> DeliverOutcome:
    """Deliver a verified COMPLETE run as a draft pull request. Never raises on refusal.

    After the draft is published the ready gate runs (unless the request skips it).
    A gate stop is recorded but is not a delivery failure: the pull request stays a draft.
    """
    deadline_s = float(getattr(request, "ci_deadline_s", DEFAULT_CI_DEADLINE_S))
    if not math.isfinite(deadline_s) or deadline_s <= 0:
        return _refused(
            DeliverRefusal(
                DeliverReason.INVALID_REQUEST,
                f"the checks deadline must be a finite number of seconds above zero, "
                f"got {deadline_s!r}",
                Path(request.run_dir),
            )
        )
    push_timeout_s = _push_timeout_s(request)
    if push_timeout_s is None:
        return _refused(
            DeliverRefusal(
                DeliverReason.INVALID_REQUEST,
                "the push timeout must be a finite number of seconds above zero, got "
                f"{getattr(request, 'push_timeout_s', None)!r}",
                Path(request.run_dir),
            )
        )
    decision = plan_delivery(request)
    if isinstance(decision, DeliverRefusal):
        return _refused(decision)
    plan = decision
    run_dir = Path(request.run_dir)
    dry_run = bool(getattr(request, "dry_run", False))
    repo_dir = getattr(request, "repo_dir", None)

    try:
        existing = read_delivery_record(run_dir)
    except ValueError as exc:
        return _outcome(
            plan,
            DeliverStatus.FAILED,
            reason=DeliverServiceReason.DELIVERY_RECORD_INVALID.value,
            message=f"delivery record for run {plan.run_id} is not trustworthy: {exc}",
        )
    if existing is not None:
        mismatched = _record_mismatches(existing, plan)
        if mismatched:
            return _outcome(
                plan,
                DeliverStatus.FAILED,
                reason=DeliverServiceReason.DELIVERY_RECORD_MISMATCH.value,
                message=(
                    f"the delivery record in {run_dir.name} does not belong to this run "
                    f"({', '.join(mismatched)} differ); it records issue "
                    f"#{existing.issue_number}: {existing.pr_url}"
                ),
                pr_url=existing.pr_url,
            )
        return _outcome(
            plan,
            DeliverStatus.ALREADY_DELIVERED,
            message=f"run {plan.run_id} was already delivered",
            pr_url=existing.pr_url,
            delivered_at=existing.delivered_at,
            created=existing.created,
            record_path=run_dir / DELIVERY_FILE,
            branch=existing.branch or plan.head_branch,
            ready=existing.ready,
            ready_reason=existing.ready_reason,
            review_level=existing.review_level,
            reviewer_route=existing.reviewer_route,
            extra_review_ran=existing.extra_review_ran,
            ci_polls=existing.ci_polls,
        )

    rendered = render_pull_request(plan)
    if dry_run:
        return _outcome(
            plan,
            DeliverStatus.DRY_RUN,
            message="dry run: nothing was pushed or created",
            title=rendered.title,
            body=rendered.body,
        )

    # The push runs the repository's pre-push hook, which takes far longer than an ordinary
    # command, so it gets its own timeout. An injected runner runs every command itself.
    push_runner = (
        None if runner is not None else functools.partial(run_command, timeout=push_timeout_s)
    )
    result = _publish(
        plan,
        rendered.title,
        rendered.body,
        runner=runner or run_command,
        push_runner=push_runner,
        cwd=Path(repo_dir) if repo_dir is not None else None,
    )
    if isinstance(result, PublishError):
        return _outcome(
            plan,
            DeliverStatus.FAILED,
            reason=result.reason.value,
            message=result.message,
            title=rendered.title,
            body=rendered.body,
        )

    active_runner = runner or run_command
    cwd = Path(repo_dir) if repo_dir is not None else None
    ready_fields: dict[str, Any] = {
        "ready": False,
        "ready_reason": READY_SKIPPED,
        "review_level": plan.review.level,
        "reviewer_route": plan.review.route_id,
        "extra_review_ran": False,
        "ci_polls": 0,
    }
    if bool(getattr(request, "run_ready_gate", True)):
        gate = run_pr_ready_gate(
            plan,
            pr_ref=result.pr_url,
            head_sha=plan.integration_commit,
            reviewer=reviewer,
            runner=active_runner,
            cwd=cwd,
            deadline_s=deadline_s,
        )
        ready_fields.update(
            ready=gate.ready,
            ready_reason=gate.reason.value if gate.reason is not None else "",
            extra_review_ran=gate.review_ran,
            ci_polls=gate.polls,
        )

    stamp = _timestamp(clock or _utc_now)
    record = DeliveryRecord(
        run_id=plan.run_id,
        issue_number=plan.issue_number,
        pr_url=result.pr_url,
        branch=result.branch,
        base_branch=plan.base_branch,
        integration_commit=plan.integration_commit,
        receipt_digest=plan.receipt_digest,
        events_digest=plan.events_digest,
        delivered_at=stamp,
        created=result.created,
        **ready_fields,
    )
    try:
        record_path = write_delivery_record(run_dir, record)
    except OSError as exc:
        return _outcome(
            plan,
            DeliverStatus.FAILED,
            reason=DeliverServiceReason.RECORD_WRITE_FAILED.value,
            message=f"pull request {result.pr_url} exists but the delivery record failed: {exc}",
            pr_url=result.pr_url,
            title=rendered.title,
            body=rendered.body,
            created=result.created,
            branch=result.branch,
            **ready_fields,
        )
    return _outcome(
        plan,
        DeliverStatus.DELIVERED,
        message=("created draft pull request" if result.created else "reused open pull request"),
        pr_url=result.pr_url,
        title=rendered.title,
        body=rendered.body,
        delivered_at=stamp,
        created=result.created,
        record_path=record_path,
        branch=result.branch,
        **ready_fields,
    )


__all__ = [
    "DELIVERY_DIGEST_FILE",
    "DELIVERY_FILE",
    "DeliverOutcome",
    "DeliverReason",
    "DeliverRunRequest",
    "DeliverServiceReason",
    "DeliverStatus",
    "DeliveryRecord",
    "deliver_run",
    "read_delivery_record",
    "write_delivery_record",
]
