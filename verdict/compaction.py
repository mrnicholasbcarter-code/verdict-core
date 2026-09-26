"""Harness-neutral semantic checkpoint/compaction lifecycle (compaction).

Canonical compaction/handoff contract shared with Continuity C03 / BOD-81.
Deterministic structured-state compaction — not an LLM summarizer and not a
second budget stack (BOD-125 owns total-window accounting).

Semantic events (adapters map brand hooks onto these):

* ``before_compact``
* ``before_yield``
* ``session_end``
* ``resume``
* ``context_pressure_checkpoint``
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import uuid4

from verdict.context_pack import estimate_tokens
from verdict.handoff import HandoffDocument

COMPACTION_SCHEMA_VERSION = "1"

SemanticEventKind = Literal[
    "before_compact", "before_yield", "session_end", "resume", "context_pressure_checkpoint"
]

SEMANTIC_EVENTS: tuple[SemanticEventKind, ...] = (
    "before_compact",
    "before_yield",
    "session_end",
    "resume",
    "context_pressure_checkpoint",
)

CompactionTrigger = Literal[
    "verified_merge",
    "major_handoff",
    "research_boundary",
    "issue_switch",
    "context_pressure",
    "timer",
]

_SEMANTIC_TRIGGERS: frozenset[str] = frozenset(
    {"verified_merge", "major_handoff", "research_boundary", "issue_switch", "context_pressure"}
)

UnitKind = Literal[
    "program_goal",
    "completed_issue",
    "pr_merge",
    "linear_story",
    "proof_requirement",
    "architectural_decision",
    "blocker_failure",
    "worktree_mapping",
    "shell_chatter",
    "mcp_duplicate",
    "search_incorporated",
    "worker_transcript",
    "abandoned_approach",
    "failure_lesson",
]

_MANDATORY_KINDS: frozenset[str] = frozenset(
    {
        "program_goal",
        "completed_issue",
        "pr_merge",
        "linear_story",
        "proof_requirement",
        "architectural_decision",
        "blocker_failure",
        "worktree_mapping",
        "failure_lesson",
    }
)

_DISCARD_KINDS: frozenset[str] = frozenset(
    {
        "shell_chatter",
        "mcp_duplicate",
        "search_incorporated",
        "worker_transcript",
        "abandoned_approach",
    }
)

_TERMINAL_STORY_STATUSES: frozenset[str] = frozenset(
    {"MERGED", "MAIN_VERIFIED", "DONE", "COMPLETED"}
)

_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class CompactionError(ValueError):
    """Raised when semantic compaction cannot preserve mandatory state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_payload(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def is_semantic_compaction_trigger(trigger: CompactionTrigger | str) -> bool:
    """True when compaction is allowed without relying on a timer alone."""
    return trigger in _SEMANTIC_TRIGGERS


@dataclass(frozen=True)
class SemanticEvent:
    """Harness-neutral lifecycle event (brand hooks map onto these kinds)."""

    kind: SemanticEventKind
    story_id: str
    trigger: CompactionTrigger
    event_id: str
    harness: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPACTION_SCHEMA_VERSION,
            "event_id": self.event_id,
            "kind": self.kind,
            "story_id": self.story_id,
            "trigger": self.trigger,
            "harness": self.harness,
            "payload": dict(self.payload),
        }


def emit_semantic_event(
    kind: SemanticEventKind,
    *,
    story_id: str,
    trigger: CompactionTrigger,
    harness: str | None = None,
    payload: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> SemanticEvent:
    if kind not in SEMANTIC_EVENTS:
        raise CompactionError("invalid_event", f"unknown semantic event kind: {kind!r}")
    if kind == "context_pressure_checkpoint" and trigger != "context_pressure":
        raise CompactionError(
            "invalid_trigger", "context_pressure_checkpoint requires trigger=context_pressure"
        )
    return SemanticEvent(
        kind=kind,
        story_id=story_id,
        trigger=trigger,
        event_id=event_id or f"evt_{uuid4().hex[:16]}",
        harness=harness,
        payload=dict(payload or {}),
    )


@dataclass(frozen=True)
class ContinuityUnit:
    """One provenance-aware continuity atom (C03-style)."""

    unit_id: str
    kind: UnitKind
    content: str
    provenance_uri: str
    mandatory: bool = False
    failure_lesson: str | None = None
    source_digest: str = ""

    def __post_init__(self) -> None:
        if not self.unit_id.strip():
            raise CompactionError("invalid_unit", "unit_id is required")
        if not self.provenance_uri.strip():
            raise CompactionError("invalid_unit", "provenance_uri is required")
        if not self.content and self.kind not in _DISCARD_KINDS:
            raise CompactionError("invalid_unit", f"empty content for {self.unit_id!r}")
        digest = self.source_digest or _digest_text(self.content)
        object.__setattr__(self, "source_digest", digest)
        if self.kind in _MANDATORY_KINDS:
            object.__setattr__(self, "mandatory", True)

    def token_count(self) -> int:
        return estimate_tokens(self.content)

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "content": self.content,
            "provenance_uri": self.provenance_uri,
            "mandatory": self.mandatory,
            "failure_lesson": self.failure_lesson,
            "source_digest": self.source_digest,
        }


@dataclass(frozen=True)
class StoryRecord:
    story_id: str
    status: str
    merge_sha: str | None = None
    pr_url: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.upper() in _TERMINAL_STORY_STATUSES and bool(self.merge_sha)

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "status": self.status,
            "merge_sha": self.merge_sha,
            "pr_url": self.pr_url,
        }


@dataclass(frozen=True)
class WorktreeBinding:
    story_id: str
    worktree: str
    branch: str
    base_sha: str
    head_sha: str
    pr_url: str | None = None
    pr_state: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_id": self.story_id,
            "worktree": self.worktree,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "pr_url": self.pr_url,
            "pr_state": self.pr_state,
        }


@dataclass(frozen=True)
class SessionContinuityState:
    """Structured continuity state compacted deterministically (not chat history)."""

    program_goal: str
    active_story: str
    next_story: str | None
    completed_stories: tuple[StoryRecord, ...]
    proof_requirements: tuple[str, ...]
    architectural_decisions: tuple[str, ...]
    blockers: tuple[str, ...]
    failure_constraints: tuple[str, ...]
    worktree: WorktreeBinding
    units: tuple[ContinuityUnit, ...] = ()

    def token_estimate(self) -> int:
        parts = [
            self.program_goal,
            self.active_story,
            self.next_story or "",
            *self.proof_requirements,
            *self.architectural_decisions,
            *self.blockers,
            *self.failure_constraints,
            _canonical(self.worktree.to_dict()),
            *(u.content for u in self.units),
        ]
        return sum(estimate_tokens(part) for part in parts if part)

    def blocks_approach(self, description: str) -> bool:
        """True when ``description`` collides with a retained failure constraint."""
        needle = {w.lower() for w in _WORD_RE.findall(description) if len(w) > 3}
        if not needle:
            return False
        for constraint in self.failure_constraints:
            hay = {w.lower() for w in _WORD_RE.findall(constraint) if len(w) > 3}
            if len(needle & hay) >= 3:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "program_goal": self.program_goal,
            "active_story": self.active_story,
            "next_story": self.next_story,
            "completed_stories": [s.to_dict() for s in self.completed_stories],
            "proof_requirements": list(self.proof_requirements),
            "architectural_decisions": list(self.architectural_decisions),
            "blockers": list(self.blockers),
            "failure_constraints": list(self.failure_constraints),
            "worktree": self.worktree.to_dict(),
            "units": [u.to_dict() for u in self.units],
        }


@dataclass(frozen=True)
class CompactionOmission:
    unit_id: str
    kind: UnitKind | str
    reason: str
    provenance_uri: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "kind": self.kind,
            "reason": self.reason,
            "provenance_uri": self.provenance_uri,
        }


@dataclass(frozen=True)
class ResumeProjection:
    active_story: str
    next_story: str | None
    worktree: str
    branch: str
    base_sha: str
    head_sha: str
    pr_url: str | None
    proof_requirements: tuple[str, ...]
    failure_constraints: tuple[str, ...]
    completed_story_ids: tuple[str, ...]
    regenerate_completed_research: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_story": self.active_story,
            "next_story": self.next_story,
            "worktree": self.worktree,
            "branch": self.branch,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "pr_url": self.pr_url,
            "proof_requirements": list(self.proof_requirements),
            "failure_constraints": list(self.failure_constraints),
            "completed_story_ids": list(self.completed_story_ids),
            "regenerate_completed_research": self.regenerate_completed_research,
        }


@dataclass(frozen=True)
class CompactionResult:
    state: SessionContinuityState
    retained: tuple[ContinuityUnit, ...]
    omissions: tuple[CompactionOmission, ...]
    tokens_before: int
    tokens_after: int
    digest: str
    compacted_text: str
    trigger: CompactionTrigger
    trigger_event: SemanticEvent | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": COMPACTION_SCHEMA_VERSION,
            "trigger": self.trigger,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "digest": self.digest,
            "retained": [u.to_dict() for u in self.retained],
            "omissions": [o.to_dict() for o in self.omissions],
            "state": self.state.to_dict(),
            "trigger_event": self.trigger_event.to_dict() if self.trigger_event else None,
        }


def _omit_reason(kind: str) -> str:
    return {
        "shell_chatter": "discard_shell_chatter",
        "mcp_duplicate": "discard_duplicate_mcp_output",
        "search_incorporated": "discard_already_incorporated_search",
        "worker_transcript": "discard_verbose_worker_transcript",
        "abandoned_approach": "discard_abandoned_approach_keep_failure_lesson",
    }.get(kind, "discard_optional_noise")


def _lesson_unit(source: ContinuityUnit) -> ContinuityUnit | None:
    lesson = (source.failure_lesson or "").strip()
    if not lesson:
        first = source.content.strip().split(".")[0].strip()
        if not first:
            return None
        lesson = first[:200]
        if not lesson.endswith("."):
            lesson += "."
    return ContinuityUnit(
        unit_id=f"lesson:{source.unit_id}",
        kind="failure_lesson",
        content=lesson,
        provenance_uri=source.provenance_uri,
        mandatory=True,
        failure_lesson=lesson,
    )


def compact_session_state(
    state: SessionContinuityState,
    *,
    trigger: CompactionTrigger,
    max_tokens: int | None = None,
    event: SemanticEvent | None = None,
) -> CompactionResult:
    """Deterministically compact structured continuity state (C03 semantics).

    Preserves mandatory goal/proof/story/worktree/decision/blocker facts.
    Discards designated noise. Converts abandoned approaches into concise
    failure constraints. Does not mutate ``state``.
    """
    if not is_semantic_compaction_trigger(trigger):
        raise CompactionError(
            "timer_alone",
            "semantic compaction requires a non-timer trigger "
            "(verified_merge, major_handoff, research_boundary, issue_switch, "
            "or context_pressure)",
        )
    if event is not None and event.trigger != trigger:
        raise CompactionError("event_mismatch", "event.trigger must match compaction trigger")

    tokens_before = state.token_estimate()
    retained: list[ContinuityUnit] = []
    omissions: list[CompactionOmission] = []
    lessons: list[str] = list(state.failure_constraints)

    for unit in state.units:
        if unit.kind in _DISCARD_KINDS or (
            not unit.mandatory and unit.kind not in _MANDATORY_KINDS
        ):
            omissions.append(
                CompactionOmission(
                    unit_id=unit.unit_id,
                    kind=unit.kind,
                    reason=_omit_reason(unit.kind),
                    provenance_uri=unit.provenance_uri,
                )
            )
            if unit.kind == "abandoned_approach":
                lesson = _lesson_unit(unit)
                if lesson is not None:
                    retained.append(lesson)
                    if lesson.content not in lessons:
                        lessons.append(lesson.content)
            continue
        retained.append(unit)

    scalar_units = _ensure_scalar_units(state, retained)
    for unit in scalar_units:
        if unit.unit_id not in {u.unit_id for u in retained}:
            retained.append(unit)

    retained_sorted = tuple(sorted(retained, key=lambda u: (u.kind, u.unit_id)))
    compacted_text = _render_compacted_text(state, retained_sorted, lessons)
    tokens_after = estimate_tokens(compacted_text)

    if max_tokens is not None:
        mandatory_cost = sum(u.token_count() for u in retained_sorted if u.mandatory)
        if mandatory_cost > max_tokens:
            raise CompactionError(
                "mandatory_overflow",
                f"mandatory continuity state requires {mandatory_cost} tokens; "
                f"budget is {max_tokens}",
            )
        if tokens_after > max_tokens:
            raise CompactionError(
                "mandatory_overflow",
                f"compacted mandatory text requires {tokens_after} tokens; budget is {max_tokens}",
            )

    new_state = replace(state, failure_constraints=tuple(lessons), units=retained_sorted)
    digest = _digest_payload(
        {
            "schema_version": COMPACTION_SCHEMA_VERSION,
            "trigger": trigger,
            "retained": [u.to_dict() for u in retained_sorted],
            "omissions": [o.to_dict() for o in omissions],
            "state": {
                "active_story": new_state.active_story,
                "next_story": new_state.next_story,
                "completed": [s.to_dict() for s in new_state.completed_stories],
                "failure_constraints": list(new_state.failure_constraints),
            },
        }
    )
    return CompactionResult(
        state=new_state,
        retained=retained_sorted,
        omissions=tuple(omissions),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        digest=digest,
        compacted_text=compacted_text,
        trigger=trigger,
        trigger_event=event,
    )


def _ensure_scalar_units(
    state: SessionContinuityState, retained: Sequence[ContinuityUnit]
) -> list[ContinuityUnit]:
    have = {u.kind for u in retained}
    extras: list[ContinuityUnit] = []
    if "program_goal" not in have and state.program_goal:
        extras.append(
            ContinuityUnit(
                unit_id="scalar:goal",
                kind="program_goal",
                content=state.program_goal,
                provenance_uri="urn:verdict:scalar:goal",
                mandatory=True,
            )
        )
    if "linear_story" not in have:
        extras.append(
            ContinuityUnit(
                unit_id="scalar:stories",
                kind="linear_story",
                content=f"active={state.active_story} next={state.next_story or ''}",
                provenance_uri="urn:verdict:scalar:linear",
                mandatory=True,
            )
        )
    if "worktree_mapping" not in have:
        wt = state.worktree
        extras.append(
            ContinuityUnit(
                unit_id="scalar:worktree",
                kind="worktree_mapping",
                content=f"{wt.story_id}|{wt.worktree}|{wt.branch}|{wt.pr_url or ''}",
                provenance_uri="urn:verdict:scalar:worktree",
                mandatory=True,
            )
        )
    for req in state.proof_requirements:
        uid = f"scalar:proof:{_digest_text(req)[:8]}"
        if not any(u.content == req for u in retained):
            extras.append(
                ContinuityUnit(
                    unit_id=uid,
                    kind="proof_requirement",
                    content=req,
                    provenance_uri=f"urn:verdict:scalar:proof:{uid}",
                    mandatory=True,
                )
            )
    for decision in state.architectural_decisions:
        uid = f"scalar:decision:{_digest_text(decision)[:8]}"
        if not any(u.content == decision for u in retained):
            extras.append(
                ContinuityUnit(
                    unit_id=uid,
                    kind="architectural_decision",
                    content=decision,
                    provenance_uri=f"urn:verdict:scalar:decision:{uid}",
                    mandatory=True,
                )
            )
    for story in state.completed_stories:
        uid = f"scalar:completed:{story.story_id}"
        if not any(u.unit_id == uid or story.story_id in u.content for u in retained):
            extras.append(
                ContinuityUnit(
                    unit_id=uid,
                    kind="completed_issue",
                    content=f"{story.story_id} {story.status} {story.merge_sha or ''}".strip(),
                    provenance_uri=f"urn:verdict:completed:{story.story_id}",
                    mandatory=True,
                )
            )
    for blocker in state.blockers:
        uid = f"scalar:blocker:{_digest_text(blocker)[:8]}"
        if not any(u.content == blocker for u in retained):
            extras.append(
                ContinuityUnit(
                    unit_id=uid,
                    kind="blocker_failure",
                    content=blocker,
                    provenance_uri=f"urn:verdict:blocker:{uid}",
                    mandatory=True,
                )
            )
    return extras


def _render_compacted_text(
    state: SessionContinuityState, retained: Sequence[ContinuityUnit], lessons: Sequence[str]
) -> str:
    lines = [
        f"program_goal: {state.program_goal}",
        f"active_story: {state.active_story}",
        f"next_story: {state.next_story or ''}",
        (
            "worktree: "
            f"{state.worktree.worktree} branch={state.worktree.branch} "
            f"base={state.worktree.base_sha} head={state.worktree.head_sha} "
            f"pr={state.worktree.pr_url or ''}"
        ),
        "completed_stories: "
        + "; ".join(
            f"{s.story_id}:{s.status}:{s.merge_sha or ''}" for s in state.completed_stories
        ),
        "proof_requirements:",
        *[f"- {item}" for item in state.proof_requirements],
        "architectural_decisions:",
        *[f"- {item}" for item in state.architectural_decisions],
        "blockers:",
        *[f"- {item}" for item in state.blockers],
        "failure_constraints:",
        *[f"- {item}" for item in lessons],
        "retained_units:",
        *[f"- [{u.kind}] {u.content}" for u in retained],
    ]
    return "\n".join(lines) + "\n"


def may_enter_ready(story_id: str, state: SessionContinuityState) -> bool:
    """Completed/merged stories must not re-enter READY after compaction."""
    sid = story_id.strip().upper()
    for story in state.completed_stories:
        if story.story_id.upper() == sid and story.is_terminal:
            return False
    for unit in state.units:
        if unit.kind != "completed_issue":
            continue
        if sid in unit.content.upper() and any(
            marker in unit.content.upper() for marker in _TERMINAL_STORY_STATUSES
        ):
            return False
    return True


def resume_projection(
    state: SessionContinuityState, *, event: SemanticEvent | None = None
) -> ResumeProjection:
    """Fresh-session projection after compaction — no regenerated completed work."""
    if event is not None and event.kind != "resume":
        raise CompactionError("invalid_event", "resume_projection expects kind=resume")
    proofs = tuple(
        dict.fromkeys(
            [
                *state.proof_requirements,
                *(u.content for u in state.units if u.kind == "proof_requirement"),
            ]
        )
    )
    return ResumeProjection(
        active_story=state.active_story,
        next_story=state.next_story,
        worktree=state.worktree.worktree,
        branch=state.worktree.branch,
        base_sha=state.worktree.base_sha,
        head_sha=state.worktree.head_sha,
        pr_url=state.worktree.pr_url,
        proof_requirements=proofs,
        failure_constraints=state.failure_constraints,
        completed_story_ids=tuple(s.story_id for s in state.completed_stories),
        regenerate_completed_research=False,
    )


def project_compacted_to_handoff(
    state: SessionContinuityState, *, worker: str = "", previous_worker: str = "none"
) -> HandoffDocument:
    """Project compacted continuity state into the shared handoff schema."""
    next_steps = [
        f"Resume {state.active_story} in {state.worktree.worktree} ({state.worktree.branch})"
    ]
    if state.next_story:
        next_steps.append(f"After completion, continue with {state.next_story}")
    if state.worktree.pr_url:
        next_steps.append(f"Continue existing PR {state.worktree.pr_url}")

    completed = [
        f"{s.story_id} {s.status}" + (f" merge={s.merge_sha}" if s.merge_sha else "")
        for s in state.completed_stories
    ]
    return HandoffDocument(
        story=state.active_story,
        worker=worker,
        worktree=state.worktree.worktree,
        branch=state.worktree.branch,
        base_sha=state.worktree.base_sha,
        current_sha=state.worktree.head_sha,
        previous_worker=previous_worker,
        objective=state.program_goal,
        completed=completed,
        currently_working_on=[state.active_story],
        next_exact_steps=next_steps,
        acceptance_criteria=list(state.proof_requirements),
        tests_proof=[],
        files_changed=[],
        contracts_changed=["verdict.compaction semantic checkpoint lifecycle (BOD-69)"],
        important_decisions=list(state.architectural_decisions),
        known_failures=list(state.failure_constraints),
        dependencies_blockers=list(state.blockers),
        do_not=list(state.failure_constraints),
    )


# Continuity instruction text shared with harness adapters (Prime/Claude/…).
CONTINUITY_INSTRUCTION = (
    "Preserve the active program/goal, completed issue/PR/merge SHA, current/next "
    "Linear story, remaining proof requirements, durable architectural decisions, "
    "blockers/failures not to repeat, and active worktree/branch/PR mappings. "
    "Discard shell chatter, duplicate MCP output, already-incorporated searches, "
    "verbose worker transcripts, and abandoned approaches unless retained as a "
    "one-line failure constraint. Checkpoints and `.verdict/handoff.md` are "
    "authoritative over chat summaries. Semantic events: before_compact, "
    "before_yield, session_end, resume, context_pressure_checkpoint."
)


__all__ = [
    "COMPACTION_SCHEMA_VERSION",
    "CONTINUITY_INSTRUCTION",
    "SEMANTIC_EVENTS",
    "CompactionError",
    "CompactionOmission",
    "CompactionResult",
    "CompactionTrigger",
    "ContinuityUnit",
    "ResumeProjection",
    "SemanticEvent",
    "SemanticEventKind",
    "SessionContinuityState",
    "StoryRecord",
    "UnitKind",
    "WorktreeBinding",
    "compact_session_state",
    "emit_semantic_event",
    "is_semantic_compaction_trigger",
    "may_enter_ready",
    "project_compacted_to_handoff",
    "resume_projection",
]
