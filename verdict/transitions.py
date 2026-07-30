"""Inspectable, fail-closed retry and fallback transition graphs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from verdict.policy import DecisionState, EligibilityCompilation, Policy, PolicyCandidate


class TransitionValidationError(ValueError):
    """Raised when an execution transition context is ambiguous or unsafe."""


class ByteState(str, Enum):
    PRE_BYTES = "pre_bytes"
    BYTES_EMITTED = "bytes_emitted"


class RetrySafety(str, Enum):
    SAFE = "safe"
    UNSAFE = "unsafe"
    UNKNOWN = "unknown"


class TransitionKind(str, Enum):
    INITIAL = "initial"
    RETRY = "retry"
    FALLBACK = "fallback"
    CHECKPOINT_RESUME = "checkpoint_resume"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class ExecutionContext:
    """State that changes during an execution attempt and gates transitions."""

    request_id: str
    idempotency_key: str | None = None
    retry_safety: RetrySafety = RetrySafety.UNKNOWN
    byte_state: ByteState = ByteState.PRE_BYTES
    checkpoint_verified: bool = False
    terminal: bool = False
    attempt: int = 0
    protocol: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not self.request_id.strip():
            raise TransitionValidationError("request_id must be non-empty")
        for name in ("retry_safety", "byte_state"):
            raw_value = getattr(self, name)
            if isinstance(raw_value, str):
                value = raw_value.lower()
                object.__setattr__(self, name, value)
            try:
                enum_type = RetrySafety if name == "retry_safety" else ByteState
                object.__setattr__(self, name, enum_type(getattr(self, name)))
            except ValueError as exc:
                raise TransitionValidationError(f"{name} is invalid") from exc
        if self.protocol is not None and not self.protocol.strip():
            raise TransitionValidationError("protocol must be non-empty when supplied")
        if type(self.checkpoint_verified) is not bool or type(self.terminal) is not bool:
            raise TransitionValidationError("checkpoint_verified and terminal must be boolean")
        if type(self.attempt) is not int or self.attempt < 0:
            raise TransitionValidationError("attempt must be a non-negative integer")


@dataclass(frozen=True)
class TransitionEdge:
    source: str
    target: str
    kind: TransitionKind
    legal: bool
    reasons: tuple[str, ...] = ()
    route_key: str | None = None
    evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind.value,
            "legal": self.legal,
            "reasons": list(self.reasons),
            "route_key": self.route_key,
            "evidence_ids": list(self.evidence_ids),
        }


@dataclass(frozen=True)
class TransitionGraph:
    policy_version: str
    request_id: str
    byte_state: ByteState
    nodes: tuple[str, ...]
    edges: tuple[TransitionEdge, ...]
    compilation: EligibilityCompilation
    compiled_at: datetime

    @property
    def legal_edges(self) -> tuple[TransitionEdge, ...]:
        return tuple(edge for edge in self.edges if edge.legal)

    @property
    def forbidden_edges(self) -> tuple[TransitionEdge, ...]:
        return tuple(edge for edge in self.edges if not edge.legal)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "request_id": self.request_id,
            "byte_state": self.byte_state.value,
            "nodes": list(self.nodes),
            "edges": [edge.to_dict() for edge in self.edges],
            "compilation": self.compilation.to_dict(),
            "compiled_at": self.compiled_at.astimezone(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


class TransitionCompiler:
    """Compile only policy-approved and retry-legal execution edges."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def compile(
        self,
        current: PolicyCandidate,
        candidates: tuple[PolicyCandidate, ...] | list[PolicyCandidate],
        context: ExecutionContext,
        *,
        at: datetime | None = None,
    ) -> TransitionGraph:
        compiled_at = _utc(at)
        all_candidates_list: list[PolicyCandidate] = []
        seen_ids: set[str] = set()
        for candidate in (current, *candidates):
            if candidate.candidate_id not in seen_ids:
                all_candidates_list.append(candidate)
                seen_ids.add(candidate.candidate_id)
        all_candidates = tuple(all_candidates_list)
        compilation = self.policy.compile(all_candidates, at=compiled_at)
        current_decision = next(
            decision
            for decision in compilation.decisions
            if decision.candidate_id == current.candidate_id
        )
        nodes = tuple(
            [
                current.candidate_id,
                *sorted(
                    item.candidate_id
                    for item in all_candidates
                    if item.candidate_id != current.candidate_id
                ),
                "terminal",
            ]
        )
        edges: list[TransitionEdge] = []
        if context.terminal:
            edges.append(
                TransitionEdge(
                    current.candidate_id,
                    "terminal",
                    TransitionKind.TERMINAL,
                    True,
                    ("execution is already terminal",),
                    current.route_key,
                    current.evidence_ids,
                )
            )
            return TransitionGraph(
                self.policy.version,
                context.request_id,
                context.byte_state,
                nodes,
                tuple(edges),
                compilation,
                compiled_at,
            )

        if current_decision.decision is not DecisionState.ALLOW:
            edges.append(
                self._edge(
                    current,
                    "terminal",
                    TransitionKind.TERMINAL,
                    False,
                    (
                        f"current route is {current_decision.decision.value}",
                        *current_decision.reasons,
                    ),
                )
            )
            return TransitionGraph(
                self.policy.version,
                context.request_id,
                context.byte_state,
                nodes,
                tuple(edges),
                compilation,
                compiled_at,
            )

        edges.append(
            self._edge(
                current,
                current.candidate_id,
                TransitionKind.INITIAL,
                True,
                ("initial attempt is policy-eligible",),
            )
        )
        for candidate in sorted(all_candidates, key=lambda item: item.candidate_id):
            if candidate.candidate_id == current.candidate_id:
                continue
            decision = next(
                item
                for item in compilation.decisions
                if item.candidate_id == candidate.candidate_id
            )
            if context.byte_state is ByteState.BYTES_EMITTED:
                legal = (
                    context.checkpoint_verified
                    and candidate.route_key == current.route_key
                    and decision.decision is DecisionState.ALLOW
                )
                reason_list: list[str] = []
                if not context.checkpoint_verified:
                    reason_list.append("verified checkpoint/resume is required after bytes")
                if candidate.route_key != current.route_key:
                    reason_list.append("model switching after bytes is forbidden")
                if decision.decision is not DecisionState.ALLOW:
                    reason_list.append("target route is not policy-eligible")
                reasons = tuple(reason_list or ["verified same-route checkpoint/resume"])
                edges.append(
                    self._edge(
                        current,
                        candidate.candidate_id,
                        TransitionKind.CHECKPOINT_RESUME,
                        legal,
                        reasons,
                        candidate,
                    )
                )
                continue

            legal, reasons = self._pre_byte_legality(current, candidate, context, decision)
            kind = (
                TransitionKind.RETRY
                if candidate.route_key == current.route_key
                else TransitionKind.FALLBACK
            )
            edges.append(
                self._edge(current, candidate.candidate_id, kind, legal, reasons, candidate)
            )
        return TransitionGraph(
            self.policy.version,
            context.request_id,
            context.byte_state,
            nodes,
            tuple(edges),
            compilation,
            compiled_at,
        )

    def _pre_byte_legality(
        self,
        current: PolicyCandidate,
        target: PolicyCandidate,
        context: ExecutionContext,
        decision: Any,
    ) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if decision.decision is not DecisionState.ALLOW:
            reasons.append(f"target route is {decision.decision.value}")
        if context.retry_safety is not RetrySafety.SAFE:
            reasons.append(f"retry safety is {context.retry_safety.value}")
        if self.policy.retry_safe_required and context.retry_safety is not RetrySafety.SAFE:
            reasons.append("policy requires an explicitly retry-safe request")
        if self.policy.allow_fallback is False and target.route_key != current.route_key:
            reasons.append("fallback is disabled by policy")
        if self.policy.require_idempotency_key and not context.idempotency_key:
            reasons.append("idempotency key is required")
        if (
            context.protocol
            and target.route_identity
            and target.route_identity.protocol != context.protocol
        ):
            reasons.append("target protocol does not match the request")
        if context.attempt >= self.policy.max_attempts:
            reasons.append("maximum attempt count reached")
        return not reasons, tuple(reasons or ["pre-byte retry/fallback is legal"])

    @staticmethod
    def _edge(
        source: PolicyCandidate,
        target: str,
        kind: TransitionKind,
        legal: bool,
        reasons: tuple[str, ...],
        target_candidate: PolicyCandidate | None = None,
    ) -> TransitionEdge:
        selected = target_candidate or source
        return TransitionEdge(
            source=source.candidate_id,
            target=target,
            kind=kind,
            legal=legal,
            reasons=tuple(dict.fromkeys(reasons)),
            route_key=selected.route_key,
            evidence_ids=selected.evidence_ids,
        )


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise TransitionValidationError("transition timestamps must be timezone-aware")
    return current.astimezone(timezone.utc)


__all__ = [
    "ByteState",
    "ExecutionContext",
    "RetrySafety",
    "TransitionCompiler",
    "TransitionEdge",
    "TransitionGraph",
    "TransitionKind",
    "TransitionValidationError",
]
