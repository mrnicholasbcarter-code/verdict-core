"""Dynamic ContextEnvelope contracts and the ContextCompiler aggregator.

Issue #257: a compiled envelope aggregates source-attributed items from many
retrieval adapters into one immutable, strict-round-trip structure that can
rebind to another model and be token-compressed without losing policy
constraints or provenance.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from verdict.capability_passports import EvidenceAuthority
from verdict.context_pack import estimate_tokens
from verdict.model_passports import ModelPassport
from verdict.models import TaskSpec

ENVELOPE_SCHEMA_VERSION = "1"

# Default authority for items whose trust level is not (yet) classified.
ITEM_AUTHORITY_UNCLASSIFIED = "unclassified"
_AUTHORITY_VALUES = frozenset({e.value for e in EvidenceAuthority}) | {ITEM_AUTHORITY_UNCLASSIFIED}

# Group field name -> expected item kind.  ``goal`` is a single item; the rest
# are homogeneous tuples.
_GROUP_KINDS = {
    "goal": "goal",
    "policy_predicates": "policy",
    "relevant_adrs": "adr",
    "verified_decisions": "decision",
    "artifacts": "artifact",
    "verification_requirements": "requirement",
}
_ITEM_KINDS = frozenset(_GROUP_KINDS.values())
_SOURCE_KINDS = frozenset(
    {"repo_file", "adr", "git", "openviking", "ruvector", "memory", "worker", "manual"}
)

# Optional groups fill order under token compression (policy/goal never drop).
_FILL_ORDER = ("verified_decisions", "relevant_adrs", "verification_requirements", "artifacts")
_ITEM_HEADER_TOKENS = 8


class ContextEnvelopeError(ValueError):
    """Raised when a context envelope artifact is malformed or unsafe."""


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 ``Z``-suffixed string."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise ContextEnvelopeError("envelope artifact must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical(value).encode()).hexdigest()}"


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextEnvelopeError(f"{name} must be a non-empty string")
    return value


def _timestamp(value: Any, name: str) -> str:
    result = _require_string(value, name)
    try:
        datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextEnvelopeError(f"{name} must be an ISO-8601 timestamp") from exc
    return result


def _strict_mapping(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContextEnvelopeError(f"{name} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ContextEnvelopeError(f"{name} missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ContextEnvelopeError(f"{name} has unknown field(s): {', '.join(sorted(unknown))}")
    return dict(value)


@dataclass(frozen=True)
class SourceRef:
    """Provenance metadata for one envelope item.

    ``ref`` carries the exact source pointer: a URL, a repository file path, a
    commit hash, or an OpenViking / RuVector URI.  ``revision`` is optional
    secondary versioning (commit hash, ADR revision, content version).
    """

    kind: str
    ref: str
    revision: str | None = None
    observed_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        _require_string(self.kind, "source.kind")
        if self.kind not in _SOURCE_KINDS:
            raise ContextEnvelopeError("source.kind is invalid")
        _require_string(self.ref, "source.ref")
        if self.revision is not None:
            _require_string(self.revision, "source.revision")
        _timestamp(self.observed_at, "source.observed_at")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "ref": self.ref,
            "observed_at": self.observed_at,
        }
        if self.revision is not None:
            payload["revision"] = self.revision
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceRef:
        payload = _strict_mapping(
            value, required={"kind", "ref", "observed_at"}, optional={"revision"}, name="source_ref"
        )
        return cls(**payload)


@dataclass(frozen=True)
class ContextItem:
    """A single source-attributed item inside a context envelope."""

    item_id: str
    kind: str
    content: str
    source: SourceRef
    confidence: float = 1.0
    authority: str = ITEM_AUTHORITY_UNCLASSIFIED

    def __post_init__(self) -> None:
        _require_string(self.item_id, "item_id")
        if self.kind not in _ITEM_KINDS:
            raise ContextEnvelopeError("item.kind is invalid")
        _require_string(self.content, "item.content")
        if not isinstance(self.source, SourceRef):
            raise ContextEnvelopeError("item.source must be a SourceRef")
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)):
            raise ContextEnvelopeError("item.confidence must be a number")
        if not 0.0 <= self.confidence <= 1.0:
            raise ContextEnvelopeError("item.confidence must be between 0 and 1")
        _require_string(self.authority, "item.authority")
        if self.authority not in _AUTHORITY_VALUES:
            raise ContextEnvelopeError("item.authority is invalid")

    @property
    def token_count(self) -> int:
        """Deterministic offline token estimate for content plus provenance."""
        return estimate_tokens(self.content) + estimate_tokens(self.source.ref)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "kind": self.kind,
            "content": self.content,
            "source": self.source.to_dict(),
            "confidence": self.confidence,
            "authority": self.authority,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextItem:
        payload = _strict_mapping(
            value,
            required={"item_id", "kind", "content", "source", "confidence"},
            optional={"authority"},
            name="context_item",
        )
        return cls(
            item_id=payload["item_id"],
            kind=payload["kind"],
            content=payload["content"],
            source=SourceRef.from_dict(payload["source"]),
            confidence=payload["confidence"],
            authority=payload.get("authority", ITEM_AUTHORITY_UNCLASSIFIED),
        )


@dataclass(frozen=True)
class ContextEnvelope:
    """Immutable compiled context for one task, safe to rebind across models.

    Every item retains a :class:`SourceRef`, so the envelope can be moved
    between models and recompiled without losing attribution.  ``token_budget``
    records the budget the envelope was compiled or optimized for, and
    ``dropped_item_ids`` records what token compression discarded (never policy).
    """

    task_id: str
    goal: ContextItem
    policy_predicates: tuple[ContextItem, ...] = ()
    relevant_adrs: tuple[ContextItem, ...] = ()
    verified_decisions: tuple[ContextItem, ...] = ()
    artifacts: tuple[ContextItem, ...] = ()
    verification_requirements: tuple[ContextItem, ...] = ()
    created_at: str = field(default_factory=utc_now_iso)
    token_budget: int | None = None
    dropped_item_ids: tuple[str, ...] = ()
    schema_version: str = ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_string(self.task_id, "task_id")
        if not isinstance(self.goal, ContextItem) or self.goal.kind != "goal":
            raise ContextEnvelopeError("goal must be a goal ContextItem")
        for group_name, expected_kind in _GROUP_KINDS.items():
            if group_name == "goal":
                continue
            items = getattr(self, group_name)
            object.__setattr__(self, group_name, tuple(items))
            for item in getattr(self, group_name):
                if not isinstance(item, ContextItem) or item.kind != expected_kind:
                    raise ContextEnvelopeError(
                        f"{group_name} must contain {expected_kind} ContextItems"
                    )
        if self.token_budget is not None and (
            isinstance(self.token_budget, bool)
            or not isinstance(self.token_budget, int)
            or self.token_budget < 1
        ):
            raise ContextEnvelopeError("token_budget must be a positive integer")
        _timestamp(self.created_at, "created_at")
        object.__setattr__(self, "dropped_item_ids", tuple(self.dropped_item_ids))
        if self.schema_version != ENVELOPE_SCHEMA_VERSION:
            raise ContextEnvelopeError("unsupported context envelope schema version")

    def iter_items(self) -> tuple[ContextItem, ...]:
        """Return every item (goal plus all groups) in deterministic order."""
        ordered: list[ContextItem] = [self.goal]
        for group_name in _GROUP_KINDS:
            if group_name == "goal":
                continue
            ordered.extend(getattr(self, group_name))
        return tuple(ordered)

    @property
    def token_count(self) -> int:
        """Total deterministic token estimate of the compiled envelope."""
        return sum(item.token_count for item in self.iter_items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "goal": self.goal.to_dict(),
            "policy_predicates": [i.to_dict() for i in self.policy_predicates],
            "relevant_adrs": [i.to_dict() for i in self.relevant_adrs],
            "verified_decisions": [i.to_dict() for i in self.verified_decisions],
            "artifacts": [i.to_dict() for i in self.artifacts],
            "verification_requirements": [i.to_dict() for i in self.verification_requirements],
            "created_at": self.created_at,
            "token_budget": self.token_budget,
            "dropped_item_ids": list(self.dropped_item_ids),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ContextEnvelope:
        payload = _strict_mapping(
            value,
            required={
                "schema_version",
                "task_id",
                "goal",
                "policy_predicates",
                "relevant_adrs",
                "verified_decisions",
                "artifacts",
                "verification_requirements",
                "created_at",
                "token_budget",
                "dropped_item_ids",
            },
            optional=set(),
            name="context_envelope",
        )
        return cls(
            task_id=payload["task_id"],
            goal=ContextItem.from_dict(payload["goal"]),
            policy_predicates=tuple(ContextItem.from_dict(i) for i in payload["policy_predicates"]),
            relevant_adrs=tuple(ContextItem.from_dict(i) for i in payload["relevant_adrs"]),
            verified_decisions=tuple(
                ContextItem.from_dict(i) for i in payload["verified_decisions"]
            ),
            artifacts=tuple(ContextItem.from_dict(i) for i in payload["artifacts"]),
            verification_requirements=tuple(
                ContextItem.from_dict(i) for i in payload["verification_requirements"]
            ),
            created_at=payload["created_at"],
            token_budget=payload["token_budget"],
            dropped_item_ids=tuple(payload["dropped_item_ids"]),
            schema_version=payload["schema_version"],
        )

    @property
    def digest(self) -> str:
        """Stable content digest of the canonical envelope representation."""
        return _digest(self.to_dict())


class ContextCompiler:
    """Aggregate source-attributed items from adapters into one envelope.

    The compiler is framework-neutral: adapters produce plain
    :class:`ContextItem` values (via the ``source_*`` helpers), and the
    compiler decides what reaches a model.  It never retrieves by itself.
    """

    def __init__(self, default_token_budget: int | None = None) -> None:
        self.default_token_budget = default_token_budget

    def compile(
        self,
        task: TaskSpec,
        *,
        sources: Mapping[str, Iterable[ContextItem]] | None = None,
        task_id: str | None = None,
        budget: int | None = None,
    ) -> ContextEnvelope:
        """Build a complete envelope from the task and any source items.

        ``sources`` maps envelope group names to their items.  A missing goal
        falls back to ``task.prompt`` with a task-derived ``SourceRef``.
        """
        if not isinstance(task, TaskSpec):
            raise ContextEnvelopeError("task must be a TaskSpec")
        groups: dict[str, list[ContextItem]] = {}
        for group_name in _GROUP_KINDS:
            groups[group_name] = []
        for group_name, items in (sources or {}).items():
            if group_name not in groups:
                raise ContextEnvelopeError(f"unknown source group: {group_name}")
            groups[group_name].extend(items)

        resolved_task_id = task_id or _digest(task.prompt)
        goal_items = groups["goal"]
        goal = (
            goal_items[0]
            if goal_items
            else self.item(
                "goal",
                task.prompt,
                SourceRef(kind="manual", ref="urn:verdict:task", revision=resolved_task_id),
            )
        )
        return ContextEnvelope(
            task_id=resolved_task_id,
            goal=goal,
            policy_predicates=tuple(groups["policy_predicates"]),
            relevant_adrs=tuple(groups["relevant_adrs"]),
            verified_decisions=tuple(groups["verified_decisions"]),
            artifacts=tuple(groups["artifacts"]),
            verification_requirements=tuple(groups["verification_requirements"]),
            token_budget=budget or self.default_token_budget,
        )

    def item(self, kind: str, content: str, source: SourceRef) -> ContextItem:
        """Build a source-attributed item with a deterministic digest id."""
        if kind not in _ITEM_KINDS:
            raise ContextEnvelopeError("item.kind is invalid")
        _require_string(content, "item.content")
        if not isinstance(source, SourceRef):
            raise ContextEnvelopeError("item.source must be a SourceRef")
        item_id = _digest({"kind": kind, "content": content, "source": source.to_dict()})[:16]
        return ContextItem(item_id=item_id, kind=kind, content=content, source=source)

    def optimize_for(
        self, envelope: ContextEnvelope, budget: int | ModelPassport
    ) -> ContextEnvelope:
        """Compress an envelope to fit ``budget`` while preserving policy.

        ``budget`` is either a raw token count or a :class:`ModelPassport`;
        for a passport the ``context_window`` is used.  Policy predicates and
        the goal are always retained.  Optional groups are filled in priority
        order (decisions, ADRs, verification requirements, artifacts) and the
        surviving items are returned with ``token_budget`` and
        ``dropped_item_ids`` recorded.
        """
        if not isinstance(envelope, ContextEnvelope):
            raise ContextEnvelopeError("envelope must be a ContextEnvelope")
        resolved = self._resolve_budget(budget)

        goal_cost = envelope.goal.token_count + _ITEM_HEADER_TOKENS
        if goal_cost > resolved:
            raise ContextEnvelopeError("token budget is too small for the task goal")
        used = goal_cost
        kept: dict[str, list[ContextItem]] = {}

        # Policy predicates are non-negotiable: always retained, even if the
        # budget is tight, because they constrain the downstream verdict.
        for group_name in ("policy_predicates",):
            for item in envelope.policy_predicates:
                kept.setdefault(group_name, []).append(item)
                used += item.token_count + _ITEM_HEADER_TOKENS
        kept.setdefault("goal", []).append(envelope.goal)

        dropped: list[str] = []
        for group_name in _FILL_ORDER:
            items = getattr(envelope, group_name)
            # Stable sort: higher confidence first, original order preserved
            # for ties so recompilation never shuffles source ordering.
            for item in sorted(items, key=lambda it: -it.confidence):
                cost = item.token_count + _ITEM_HEADER_TOKENS
                if used + cost <= resolved:
                    kept.setdefault(group_name, []).append(item)
                    used += cost
                else:
                    dropped.append(item.item_id)
            kept.setdefault(group_name, [])

        return replace(
            envelope,
            goal=kept["goal"][0],
            policy_predicates=tuple(kept["policy_predicates"]),
            relevant_adrs=tuple(kept["relevant_adrs"]),
            verified_decisions=tuple(kept["verified_decisions"]),
            artifacts=tuple(kept["artifacts"]),
            verification_requirements=tuple(kept["verification_requirements"]),
            token_budget=resolved,
            dropped_item_ids=tuple(dropped),
        )

    def _resolve_budget(self, budget: int | ModelPassport) -> int:
        if isinstance(budget, bool):
            raise ContextEnvelopeError("token budget must be a positive integer")
        if isinstance(budget, int):
            if budget < 1:
                raise ContextEnvelopeError("token budget must be positive")
            return budget
        if isinstance(budget, ModelPassport):
            window = budget.context_window
            if isinstance(window, int) and window > 0:
                return window
            if self.default_token_budget:
                return self.default_token_budget
            raise ContextEnvelopeError("passport declares no usable context window")
        raise ContextEnvelopeError("budget must be a token count or ModelPassport")

    @staticmethod
    def source_repo_file(path: str, *, revision: str | None = None) -> SourceRef:
        return SourceRef(kind="repo_file", ref=path, revision=revision)

    @staticmethod
    def source_adr(path: str, *, revision: str | None = None) -> SourceRef:
        return SourceRef(kind="adr", ref=path, revision=revision)

    @staticmethod
    def source_git(commit: str) -> SourceRef:
        return SourceRef(kind="git", ref=f"commit:{commit}", revision=commit)

    @staticmethod
    def source_openviking(uri: str) -> SourceRef:
        return SourceRef(kind="openviking", ref=uri)

    @staticmethod
    def source_ruvector(uri: str) -> SourceRef:
        return SourceRef(kind="ruvector", ref=uri)


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "ITEM_AUTHORITY_UNCLASSIFIED",
    "ContextCompiler",
    "ContextEnvelope",
    "ContextEnvelopeError",
    "ContextItem",
    "SourceRef",
    "utc_now_iso",
]
