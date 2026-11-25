"""One task profile on both sides: ``prepared_task_profile`` in assigned mode.

The assigned lane mints a route-bound proof over the profile digest and the relay recomputes the
profile to check it.  Both call this one helper, so the lane's digest and the relay's can never
differ.  In assigned mode no text decides anything: the planner is not consulted, requirements
come from request fields only, and the task family is the constant ``agentic``.
"""

from __future__ import annotations

from typing import Any

import pytest

from verdict.context_pack import ContextPlan, estimate_tokens
from verdict.intelligence import prepared_task_profile

TOOLS = [{"type": "function", "function": {"name": "run"}}]


class _PlannerThatMustNotRun:
    def plan(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the planner must not be consulted in assigned mode")


class _PlannerAddingToolCalling:
    """What the real planner does for text with words like 'implement' or 'test'."""

    calls = 0

    def plan(self, *_args: Any, **_kwargs: Any) -> Any:
        type(self).calls += 1

        class _Spec:
            required_capabilities = ("tool-calling",)

        class _Plan:
            task_spec = _Spec()

        return _Plan()


def _assigned(task: str, context: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    return prepared_task_profile(task, dict(context or {}), assigned=True, **kwargs)


def test_the_planner_is_never_called_in_assigned_mode() -> None:
    _assigned("implement the parser", planner=_PlannerThatMustNotRun())  # type: ignore[arg-type]


def test_the_unassigned_mode_still_consults_the_planner() -> None:
    _PlannerAddingToolCalling.calls = 0
    requirements, _ = prepared_task_profile(
        "implement the parser",
        {},
        planner=_PlannerAddingToolCalling(),  # type: ignore[arg-type]
    )
    assert _PlannerAddingToolCalling.calls == 1
    assert requirements.names == ("tools",)  # the control: text does add tool-calling here


@pytest.mark.parametrize(
    "text",
    [
        "implement the parser",
        "build the thing",
        "call the api",
        "run the test suite",
        "implement, build and test the api",
    ],
)
def test_words_in_the_text_never_add_tool_calling(text: str) -> None:
    requirements, profile = _assigned(text, planner=_PlannerAddingToolCalling())
    assert requirements.names == ()
    assert profile.required_capabilities == ()


def test_the_request_fields_still_raise_requirements() -> None:
    requirements, profile = _assigned(
        "review the docs", {"tools": TOOLS, "reasoning_required": True}
    )
    assert requirements.names == ("tools", "reasoning")
    assert profile.required_capabilities == ("tools", "reasoning")


@pytest.mark.parametrize(
    "task", ["review the code", "plan the migration", "write the docs", "fix the bug", "hello"]
)
@pytest.mark.parametrize("field", [None, "reasoning", "general", "research", ""])
def test_the_family_is_agentic_whatever_the_text_or_a_task_family_field_says(
    task: str, field: str | None
) -> None:
    context: dict[str, Any] = {} if field is None else {"task_family": field}
    _, profile = _assigned(task, context)
    assert profile.task_family == "agentic"


def _estimate(text: str, profile: Any, *, family: str | None = None) -> int | None:
    """What ``_estimate_candidate_context_plans`` computes for one candidate."""
    plan = ContextPlan.estimate_for_candidate(
        task=text,
        candidate_id="omniroute/route",
        context_window=200_000,
        task_family=profile.task_family if family is None else family,
        required_capabilities=profile.required_capabilities,
        criteria_count=0,
    )
    return plan.estimated_input_tokens


def test_texts_that_differ_only_in_such_words_get_the_agentic_allowance() -> None:
    for text in ("review the plan and the docs", "implement the build and test the api"):
        _, profile = _assigned(text)
        assert profile.task_family == "agentic"
        assert _estimate(text, profile) == _estimate(text, profile, family="agentic")
    # The control: unassigned, the same words change the family and so the estimate.
    _, unassigned = prepared_task_profile("review the plan and the docs", {})
    assert unassigned.task_family != "agentic"


def test_texts_of_the_same_length_get_the_same_estimate() -> None:
    keyword_text = "implement the build and test the api "
    plain_text = "q" * len(keyword_text)
    assert len(keyword_text) == len(plain_text)
    assert estimate_tokens(keyword_text) == estimate_tokens(plain_text)
    _, keyword_profile = _assigned(keyword_text)
    _, plain_profile = _assigned(plain_text)
    assert _estimate(keyword_text, keyword_profile) == _estimate(plain_text, plain_profile)
    # The control: unassigned, equal lengths no longer give equal estimates (the family differs).
    _, k = prepared_task_profile(keyword_text, {})
    _, p = prepared_task_profile(plain_text, {})
    assert _estimate(keyword_text, k) != _estimate(plain_text, p)


@pytest.mark.parametrize("field", ["stream", "streaming_required"])
def test_streaming_stays_in_the_requirements_and_the_digest(field: str) -> None:
    with_streaming, profile = _assigned("do the task", {field: True})
    without, plain = _assigned("do the task", {})
    assert "streaming" in with_streaming.names
    assert "streaming" in profile.required_capabilities
    assert "streaming" not in without.names
    # The waiver is applied later, to the gate's copy: the digest still binds the requirement.
    assert profile.digest != plain.digest


def test_the_same_task_and_context_give_the_same_digest_from_two_calls() -> None:
    context = {"tools": TOOLS, "stream": True}
    first = _assigned("do the task", context)[1]
    second = _assigned("do the task", context)[1]
    assert first.digest == second.digest
    assert first.digest.startswith("sha256:")


def test_a_different_task_text_changes_the_digest() -> None:
    assert _assigned("do the task")[1].digest != _assigned("do the other task")[1].digest


def test_assigned_and_unassigned_digests_differ_so_the_two_sides_must_agree_on_the_mode() -> None:
    assert _assigned("do the task")[1].digest != prepared_task_profile("do the task", {})[1].digest


def test_assigned_mode_does_not_change_the_callers_context() -> None:
    context: dict[str, Any] = {"tools": TOOLS, "task_family": "reasoning"}
    before = dict(context)
    _assigned("do the task", context)
    prepared_task_profile("do the task", context, assigned=True)
    assert context == before


# --- unassigned mode is exactly today's result ---------------------------------------------
# Recorded from the unchanged helper before the assigned mode was added
# (see the evidence log of the build): the default call must not move.

RECORDED = [
    pytest.param(
        "implement the parser and add a test for the api",
        {},
        "sha256:23123a858bcc85160c5f5cf8398917c746f23203c2f8d6999b27ec52f28f7946",
        "code_edit",
        ("tools",),
        ("planner:tool-calling",),
        id="text-implies-tools",
    ),
    pytest.param(
        "review the docs",
        {"stream": True, "tools": TOOLS},
        "sha256:2297690e75375e70bc69dffdf948f287953726f91dbd9dc16c09e88829c1aa4a",
        "code_review",
        ("tools", "streaming"),
        ("request.tools", "request.streaming"),
        id="tools-and-stream-fields",
    ),
    pytest.param(
        "summarize the plan",
        {
            "task_family": "reasoning",
            "streaming_required": True,
            "_relay_protocol": "anthropic.messages",
        },
        "sha256:bb7f2284da7210c2393775a9192e941f134b6a4465786322d0d3e49f8d91855b",
        "reasoning",
        ("streaming",),
        ("request.streaming",),
        id="family-field-and-messages-protocol",
    ),
]


@pytest.mark.parametrize(("task", "context", "digest", "family", "names", "reasons"), RECORDED)
def test_unassigned_mode_gives_exactly_todays_result(
    task: str,
    context: dict[str, Any],
    digest: str,
    family: str,
    names: tuple[str, ...],
    reasons: tuple[str, ...],
) -> None:
    requirements, profile = prepared_task_profile(task, dict(context))
    assert profile.digest == digest
    assert profile.task_family == family
    assert requirements.names == names
    assert requirements.reasons == reasons
    # An explicit assigned=False is the default.
    assert prepared_task_profile(task, dict(context), assigned=False)[1].digest == digest
