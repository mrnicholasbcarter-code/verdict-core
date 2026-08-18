"""Shared Hive Workspace: quarantined worker proposals and shared knowledge.

Issue #257: worker results enter as unverified ``proposal`` records.  Only a
successful policy verification promotes a proposal to ``verified`` and commits
it to the shared :class:`MemoryPlane`.  Anything unverified stays quarantined
as ``proposal`` — it is never admitted to shared knowledge.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from verdict.memory_plane import MemoryPlane, MemoryRecord

PROPOSAL_SCHEMA_VERSION = "1"
_VERIFIED_TRUST = "hive-verified"
_NAMESPACE = "hive-workspace"
_SCOPE = "default"

PROPOSAL_PENDING = "pending"
PROPOSAL_VERIFIED = "verified"
PROPOSAL_REJECTED = "rejected"

ProposalState = str
VALID_STATES = (PROPOSAL_PENDING, PROPOSAL_VERIFIED, PROPOSAL_REJECTED)


@dataclass(frozen=True)
class WorkerResult:
    """A source-attributed result produced by one worker."""

    worker_id: str
    content: str
    result_type: str = "artifact"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("content must be non-empty")
        if not isinstance(self.result_type, str) or not self.result_type.strip():
            raise ValueError("result_type must be non-empty")
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "content": self.content,
            "result_type": self.result_type,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> WorkerResult:
        if not isinstance(value, Mapping):
            raise ValueError("worker_result must be an object")
        return cls(
            worker_id=value.get("worker_id", ""),
            content=value.get("content", ""),
            result_type=value.get("result_type", "artifact"),
            metadata=value.get("metadata", {}),
        )


@dataclass(frozen=True)
class Proposal:
    """An immutable worker result in a quarantined workspace state."""

    proposal_id: str
    task_id: str
    worker_result: WorkerResult
    status: ProposalState = PROPOSAL_PENDING
    reason: str | None = None
    schema_version: str = PROPOSAL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.proposal_id, str) or not self.proposal_id.strip():
            raise ValueError("proposal_id must be non-empty")
        if not isinstance(self.task_id, str) or not self.task_id.strip():
            raise ValueError("task_id must be non-empty")
        if not isinstance(self.worker_result, WorkerResult):
            raise ValueError("worker_result must be a WorkerResult")
        if self.status not in VALID_STATES:
            raise ValueError(f"proposal status is invalid: {self.status}")
        if self.status == PROPOSAL_REJECTED and not self.reason:
            raise ValueError("rejected proposals require a reason")
        if self.reason is not None and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise ValueError("reason must be non-empty")

    @property
    def verified(self) -> bool:
        return self.status == PROPOSAL_VERIFIED

    def with_status(self, status: ProposalState, *, reason: str | None = None) -> Proposal:
        """Return a transitioned copy; resolved proposals cannot be re-opened."""
        if self.status == status:
            return self
        if self.status != PROPOSAL_PENDING:
            raise ValueError("cannot transition a resolved proposal")
        if status == PROPOSAL_PENDING:
            raise ValueError("cannot re-open a resolved proposal")
        return replace(self, status=status, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "worker_result": self.worker_result.to_dict(),
            "status": self.status,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Proposal:
        if not isinstance(value, Mapping):
            raise ValueError("proposal must be an object")
        return cls(
            proposal_id=value.get("proposal_id", ""),
            task_id=value.get("task_id", ""),
            worker_result=WorkerResult.from_dict(value.get("worker_result", {})),
            status=value.get("status", PROPOSAL_PENDING),
            reason=value.get("reason"),
            schema_version=value.get("schema_version", PROPOSAL_SCHEMA_VERSION),
        )


# Verification gate: returns True to admit a proposal to shared knowledge, or
# raises a ValueError carrying the rejection reason.
VerificationGate = Callable[[Proposal], bool]


@dataclass(frozen=True)
class VerificationOutcome:
    """Result of a policy-verification pass over one proposal."""

    proposal: Proposal
    passed: bool
    reason: str | None = None

    @property
    def resulting_proposal(self) -> Proposal:
        if self.passed:
            return self.proposal.with_status(PROPOSAL_VERIFIED, reason=self.reason or "verified")
        return self.proposal.with_status(PROPOSAL_REJECTED, reason=self.reason or "rejected")


@dataclass(frozen=True)
class _ProposalRecord:
    """Latest durable row for one proposal id."""

    proposal: Proposal
    record: MemoryRecord


class HiveWorkspace:
    """Quarantine-then-commit pipeline for worker proposals.

    Submissions are stored as unverified ``proposal`` records in the shared
    memory plane.  Verification promotes the durable record to the
    ``hive-verified`` trust level; rejection writes a privacy-safe tombstone.
    Quarantined proposals are never admitted to shared knowledge retrieval.
    """

    def __init__(
        self,
        memory: MemoryPlane,
        *,
        namespace: str = _NAMESPACE,
        scope: str = _SCOPE,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.memory = memory
        self.namespace = namespace
        self.scope = scope
        self._clock = clock or _default_clock

    def submit_proposal(
        self, task_id: str, worker_result: WorkerResult, proposal_id: str | None = None
    ) -> Proposal:
        """Quarantine a worker result as an unverified pending proposal."""
        actual_id = proposal_id or f"proposal:{_default_clock():.0f}:{task_id}"
        current = self._latest(actual_id)
        if current is not None and current.proposal.status == PROPOSAL_PENDING:
            raise ValueError(f"pending proposal already exists: {actual_id}")
        proposal = Proposal(
            proposal_id=actual_id,
            task_id=task_id,
            worker_result=worker_result,
            status=PROPOSAL_PENDING,
        )
        self.memory.put(self._record_for(proposal))
        return proposal

    def verify(self, proposal: Proposal, gate: VerificationGate) -> VerificationOutcome:
        """Run policy verification and commit or quarantine the proposal."""
        if proposal.status != PROPOSAL_PENDING:
            raise ValueError("only pending proposals can be verified")
        reason: str | None
        try:
            passed = bool(gate(proposal))
        except ValueError as exc:
            passed = False
            reason = str(exc)
        else:
            reason = None
        if passed:
            if reason is None:
                reason = "verified"
            resulting = proposal.with_status(PROPOSAL_VERIFIED, reason=reason)
            self._commit_verified(resulting)
            return VerificationOutcome(resulting, passed=True, reason=reason)
        reason = reason or "verification_gate_rejected"
        resulting = proposal.with_status(PROPOSAL_REJECTED, reason=reason)
        self._commit_rejected(resulting)
        return VerificationOutcome(resulting, passed=False, reason=reason)

    def get(self, proposal_id: str) -> Proposal | None:
        """Return the latest proposal by id, or None when absent."""
        current = self._latest(proposal_id)
        return current.proposal if current is not None else None

    def pending(self) -> list[Proposal]:
        """Return all proposals still quarantined as ``pending``."""
        return [item.proposal for item in self._all() if item.proposal.status == PROPOSAL_PENDING]

    def verified(self) -> list[Proposal]:
        """Return all proposals admitted to shared knowledge."""
        return [item.proposal for item in self._all() if item.proposal.verified]

    def _latest(self, proposal_id: str) -> _ProposalRecord | None:
        candidates = [item for item in self._all() if item.proposal.proposal_id == proposal_id]
        return max(candidates, key=lambda item: item.record.created_at) if candidates else None

    def _all(self) -> list[_ProposalRecord]:
        records = self.memory.records(
            namespace=self.namespace, scope=self.scope, include_history=True
        )
        latest: dict[str, _ProposalRecord] = {}
        for record in records:
            meta = record.metadata
            proposal_id = meta.get("proposal_id") if isinstance(meta, Mapping) else None
            if not isinstance(proposal_id, str) or not isinstance(meta, Mapping):
                continue
            try:
                proposal = Proposal.from_dict(meta)
            except ValueError:
                continue
            current = latest.get(proposal_id)
            if current is None or record.created_at > current.record.created_at:
                latest[proposal_id] = _ProposalRecord(proposal, record)
        return sorted(latest.values(), key=lambda item: item.proposal.proposal_id)

    def _record_for(self, proposal: Proposal) -> MemoryRecord:
        now = self._clock()
        return MemoryRecord(
            record_id=proposal.proposal_id,
            namespace=self.namespace,
            key=f"proposal:{proposal.task_id}",
            content=f"proposal:{proposal.proposal_id}",
            source=f"worker:{proposal.worker_result.worker_id}",
            trust="hive-quarantine",
            scope=self.scope,
            metadata=proposal.to_dict(),
            created_at=now,
            updated_at=now,
            authority="hive-workspace",
            confidence=1.0,
            sensitivity="standard",
            provenance={
                "workspace": "shared",
                "schema_version": PROPOSAL_SCHEMA_VERSION,
                "task_id": proposal.task_id,
            },
        )

    def _commit_verified(self, proposal: Proposal) -> None:
        current = self._latest(proposal.proposal_id)
        base = self._record_for(proposal)
        base = replace(
            base,
            record_id=f"verified:{proposal.proposal_id}",
            content=proposal.worker_result.content,
            trust=_VERIFIED_TRUST,
            authority="hive-policy",
            authority_verified=True,
            confidence=proposal.worker_result.metadata.get("confidence", 1.0),
            supersedes=current.record.record_id if current is not None else None,
        )
        self.memory.put_verified(base)

    def _commit_rejected(self, proposal: Proposal) -> None:
        current = self._latest(proposal.proposal_id)
        if current is None:
            return
        # Privacy-safe marker: no copy of the rejected content.
        now = self._clock()
        tombstone = MemoryRecord(
            record_id=f"rejected:{proposal.proposal_id}",
            namespace=self.namespace,
            key=f"proposal:{proposal.task_id}",
            content="[rejected-proposal]",
            source="hive-workspace",
            trust="hive-rejected",
            scope=self.scope,
            metadata=proposal.to_dict(),
            created_at=now,
            updated_at=now,
            supersedes=current.record.record_id,
            authority="hive-policy",
            sensitivity="standard",
            provenance={
                "workspace": "shared",
                "schema_version": PROPOSAL_SCHEMA_VERSION,
                "task_id": proposal.task_id,
            },
            confidence=0.0,
            status="tombstone",
        )
        self.memory.put(tombstone)


def _default_clock() -> float:
    import time

    return time.time()


__all__ = [
    "PROPOSAL_PENDING",
    "PROPOSAL_REJECTED",
    "PROPOSAL_VERIFIED",
    "HiveWorkspace",
    "Proposal",
    "ProposalState",
    "VerificationGate",
    "VerificationOutcome",
    "WorkerResult",
]
