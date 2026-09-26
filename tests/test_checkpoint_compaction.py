"""proof: deterministic semantic checkpoint/compaction lifecycle."""

from __future__ import annotations

import copy

import pytest

from verdict.compaction import (
    SEMANTIC_EVENTS,
    CompactionError,
    CompactionTrigger,
    ContinuityUnit,
    SessionContinuityState,
    StoryRecord,
    WorktreeBinding,
    compact_session_state,
    emit_semantic_event,
    is_semantic_compaction_trigger,
    may_enter_ready,
    project_compacted_to_handoff,
    resume_projection,
)
from verdict.context_pack import estimate_tokens
from verdict.handoff import parse_handoff


def _binding() -> WorktreeBinding:
    return WorktreeBinding(
        story_id="BOD-100",
        worktree="/tmp/wt-bod-100",
        branch="feat/bod-100",
        base_sha="abc111",
        head_sha="def222",
        pr_url="https://github.com/example/repo/pull/42",
        pr_state="OPEN",
    )


def _noisy_state() -> SessionContinuityState:
    binding = _binding()
    return SessionContinuityState(
        program_goal="Ship harness continuity without losing proof state",
        active_story="BOD-100",
        next_story="BOD-101",
        completed_stories=(
            StoryRecord(
                story_id="BOD-99",
                status="MERGED",
                merge_sha="deadbeef01",
                pr_url="https://github.com/example/repo/pull/41",
            ),
        ),
        proof_requirements=("retain mandatory goal/proof", "exclude designated noise"),
        architectural_decisions=("One canonical compaction contract shared with Continuity C03",),
        blockers=("Linear MCP write may need retry",),
        failure_constraints=(),
        worktree=_binding(),
        units=(
            ContinuityUnit(
                unit_id="goal",
                kind="program_goal",
                content="Ship harness continuity without losing proof state",
                provenance_uri="urn:verdict:goal",
                mandatory=True,
            ),
            ContinuityUnit(
                unit_id="proof",
                kind="proof_requirement",
                content="REQUIRED_FACT=keep-proof-digest",
                provenance_uri="urn:verdict:proof",
                mandatory=True,
            ),
            ContinuityUnit(
                unit_id="decision",
                kind="architectural_decision",
                content="One canonical compaction contract shared with Continuity C03",
                provenance_uri="urn:verdict:adr",
                mandatory=True,
            ),
            ContinuityUnit(
                unit_id="story-map",
                kind="worktree_mapping",
                content=f"{binding.story_id}|{binding.worktree}|{binding.branch}|{binding.pr_url}",
                provenance_uri="urn:verdict:worktree",
                mandatory=True,
            ),
            ContinuityUnit(
                unit_id="completed",
                kind="completed_issue",
                content="BOD-99 MERGED deadbeef01",
                provenance_uri="urn:verdict:completed:BOD-99",
                mandatory=True,
            ),
            ContinuityUnit(
                unit_id="next",
                kind="linear_story",
                content="active=BOD-100 next=BOD-101",
                provenance_uri="urn:verdict:linear",
                mandatory=True,
            ),
            ContinuityUnit(
                unit_id="shell-1",
                kind="shell_chatter",
                content="ls -la /tmp && echo noop " * 40,
                provenance_uri="urn:noise:shell",
            ),
            ContinuityUnit(
                unit_id="mcp-dup",
                kind="mcp_duplicate",
                content='{"tools":[{"name":"search"}]} ' * 30,
                provenance_uri="urn:noise:mcp",
            ),
            ContinuityUnit(
                unit_id="search-done",
                kind="search_incorporated",
                content="already folded into decision: use C03 semantics " * 20,
                provenance_uri="urn:noise:search",
            ),
            ContinuityUnit(
                unit_id="worker-log",
                kind="worker_transcript",
                content="verbose worker thought stream " * 50,
                provenance_uri="urn:noise:worker",
            ),
            ContinuityUnit(
                unit_id="bad-approach",
                kind="abandoned_approach",
                content=(
                    "Tried rewriting the budget governor inside compaction; "
                    "rejected because BOD-125 owns budget accounting."
                ),
                provenance_uri="urn:noise:approach",
                failure_lesson="Do not reinvent budget accounting inside compaction (BOD-125 owns it).",
            ),
        ),
    )


def test_semantic_events_are_harness_neutral() -> None:
    assert SEMANTIC_EVENTS == (
        "before_compact",
        "before_yield",
        "session_end",
        "resume",
        "context_pressure_checkpoint",
    )
    event = emit_semantic_event(
        "before_compact",
        story_id="BOD-100",
        trigger="verified_merge",
        payload={"source": "fixture"},
    )
    assert event.kind == "before_compact"
    assert event.trigger == "verified_merge"
    assert event.harness is None
    assert event.event_id.startswith("evt_")


@pytest.mark.parametrize(
    ("trigger", "allowed"),
    [
        ("verified_merge", True),
        ("major_handoff", True),
        ("research_boundary", True),
        ("issue_switch", True),
        ("context_pressure", True),
        ("timer", False),
    ],
)
def test_compaction_triggers_are_semantic_not_timer_alone(
    trigger: CompactionTrigger, allowed: bool
) -> None:
    assert is_semantic_compaction_trigger(trigger) is allowed


def test_pre_post_compaction_retains_mandatory_excludes_noise() -> None:
    state = _noisy_state()
    original = copy.deepcopy(state)
    before_tokens = state.token_estimate()

    result = compact_session_state(state, trigger="context_pressure")

    retained_ids = {u.unit_id for u in result.retained}
    omitted_ids = {o.unit_id for o in result.omissions}
    assert "goal" in retained_ids
    assert "proof" in retained_ids
    assert "decision" in retained_ids
    assert "story-map" in retained_ids
    assert "completed" in retained_ids
    assert "next" in retained_ids
    assert "REQUIRED_FACT=keep-proof-digest" in result.compacted_text
    assert "shell-1" in omitted_ids
    assert "mcp-dup" in omitted_ids
    assert "search-done" in omitted_ids
    assert "worker-log" in omitted_ids
    assert "bad-approach" in omitted_ids
    # Failed approach survives only as a concise constraint.
    assert any("BOD-125" in c for c in result.state.failure_constraints)
    assert "Tried rewriting the budget governor" not in result.compacted_text
    assert result.tokens_after < result.tokens_before
    assert result.tokens_after < before_tokens
    assert result.digest
    assert result.omissions
    # Original input unchanged (C03 semantics).
    assert state == original


def test_resume_continues_correct_worktree_and_pr() -> None:
    result = compact_session_state(_noisy_state(), trigger="major_handoff")
    projection = resume_projection(result.state)
    assert projection.active_story == "BOD-100"
    assert projection.next_story == "BOD-101"
    assert projection.worktree == "/tmp/wt-bod-100"
    assert projection.branch == "feat/bod-100"
    assert projection.pr_url == "https://github.com/example/repo/pull/42"
    assert "REQUIRED_FACT=keep-proof-digest" in projection.proof_requirements
    assert projection.regenerate_completed_research is False


def test_merged_story_cannot_reenter_ready() -> None:
    result = compact_session_state(_noisy_state(), trigger="verified_merge")
    assert may_enter_ready("BOD-99", result.state) is False
    assert may_enter_ready("BOD-100", result.state) is True
    assert may_enter_ready("BOD-101", result.state) is True


def test_failure_lesson_blocks_identical_retry() -> None:
    result = compact_session_state(_noisy_state(), trigger="research_boundary")
    constraints = " ".join(result.state.failure_constraints)
    assert "BOD-125" in constraints
    assert result.state.blocks_approach("rewrite budget governor inside compaction")


def test_context_pressure_event_and_bounded_provenance() -> None:
    state = _noisy_state()
    event = emit_semantic_event(
        "context_pressure_checkpoint", story_id=state.active_story, trigger="context_pressure"
    )
    result = compact_session_state(state, trigger="context_pressure", event=event)
    assert result.trigger_event is not None
    assert result.trigger_event.kind == "context_pressure_checkpoint"
    for unit in result.retained:
        assert unit.provenance_uri.startswith("urn:")
        assert unit.source_digest
    assert len(result.compacted_text) < 8_000


def test_overflow_blocks_when_mandatory_cannot_fit() -> None:
    unit = ContinuityUnit(
        unit_id="huge-goal",
        kind="program_goal",
        content="MANDATORY " * 5000,
        provenance_uri="urn:verdict:goal",
        mandatory=True,
    )
    state = SessionContinuityState(
        program_goal=unit.content,
        active_story="BOD-1",
        next_story=None,
        completed_stories=(),
        proof_requirements=(),
        architectural_decisions=(),
        blockers=(),
        failure_constraints=(),
        worktree=_binding(),
        units=(unit,),
    )
    with pytest.raises(CompactionError) as exc:
        compact_session_state(state, trigger="context_pressure", max_tokens=10)
    assert exc.value.code == "mandatory_overflow"


def test_project_compacted_state_into_handoff() -> None:
    from verdict.handoff import render_handoff

    result = compact_session_state(_noisy_state(), trigger="issue_switch")
    rendered = project_compacted_to_handoff(result.state, worker="cursor")
    md = render_handoff(rendered)
    loaded = parse_handoff(md)
    assert loaded.story == "BOD-100"
    assert loaded.worktree == "/tmp/wt-bod-100"
    assert loaded.branch == "feat/bod-100"
    assert any("BOD-101" in step for step in loaded.next_exact_steps)
    assert any("BOD-125" in item for item in loaded.do_not)
    assert any(
        "keep-proof" in item or "proof" in item.lower() for item in loaded.acceptance_criteria
    )
    assert estimate_tokens(md) < estimate_tokens("x" * 50_000)


def test_resume_event_resolves_same_stories() -> None:
    compacted = compact_session_state(_noisy_state(), trigger="issue_switch")
    resume_event = emit_semantic_event(
        "resume", story_id=compacted.state.active_story, trigger="issue_switch"
    )
    projection = resume_projection(compacted.state, event=resume_event)
    assert projection.active_story == "BOD-100"
    assert projection.next_story == "BOD-101"
    assert resume_event.kind == "resume"
