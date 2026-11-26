"""Single entry point for ``verdict deliver``: gate, render, publish, record.

``deliver_run`` composes the pure eligibility gate (``deliver``), the pure
renderer (``deliver_render``) and the git/``gh`` adapter (``deliver_publish``):

1. a refused run returns a ``REFUSED`` outcome and never reaches the publisher;
2. a run already delivered (a valid ``delivery.json``) returns the recorded PR
   without any command, so a repeated deliver is idempotent;
3. ``dry_run`` returns the plan, title and body and runs no command at all;
4. otherwise the publisher runs, and only on success is the delivery record
   (``delivery.json`` plus ``delivery.sha256``) written next to the run's durable
   state. ``events.jsonl`` and ``receipt.json`` are never written.

See the ``verdict-deliver`` OpenSpec change.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
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
from verdict.orchestration.deliver_publish import CommandRunner, PublishError, run_command
from verdict.orchestration.deliver_publish import publish_branch_and_pr as _publish
from verdict.orchestration.deliver_render import render_pull_request

DELIVERY_FILE = "delivery.json"
DELIVERY_DIGEST_FILE = "delivery.sha256"
DELIVERY_SCHEMA = "verdict.delivery/1"

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
    )


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


def deliver_run(
    request: DeliverRunRequest | DeliverRequest,
    *,
    runner: CommandRunner | None = None,
    clock: Clock | None = None,
) -> DeliverOutcome:
    """Deliver a verified COMPLETE run as a draft pull request. Never raises on refusal."""
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
        if existing.issue_number != plan.issue_number:
            return _outcome(
                plan,
                DeliverStatus.FAILED,
                reason=DeliverServiceReason.DELIVERY_RECORD_MISMATCH.value,
                message=(
                    f"run {plan.run_id} was already delivered for issue "
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

    result = _publish(
        plan,
        rendered.title,
        rendered.body,
        runner=runner or run_command,
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
