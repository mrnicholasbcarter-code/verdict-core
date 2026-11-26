"""Draft pull request rendering for ``verdict deliver`` (in-memory plans only)."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from verdict.orchestration.deliver import (
    DeliverPlan,
    NodeSummary,
    ReviewEvidence,
    VerificationResult,
)
from verdict.orchestration.deliver_render import (
    MAX_BODY_LENGTH,
    MAX_TITLE_LENGTH,
    TRUNCATION_MARKER,
    NodeDetail,
    RenderedPullRequest,
    render_pull_request,
)

_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[\s:]*"
    r"(?:[\w.-]+/[\w.-]+)?#\d+",
    re.IGNORECASE,
)
_CODE_SPAN = re.compile(r"(?<!\\)`[^`\n]*`")
_AT_NAME = re.compile(r"@[A-Za-z0-9_]")


class _Mention:
    """A live mention is an ``@name`` that is not inside an unescaped Markdown code span.

    Whatever precedes the ``@`` (a backtick, a letter, punctuation) never makes it inert.
    """

    @staticmethod
    def search(text: str) -> re.Match[str] | None:
        return _AT_NAME.search(_CODE_SPAN.sub("", text))


_MENTION = _Mention()


def _nodes() -> tuple[NodeSummary, ...]:
    return (
        NodeSummary(
            node_id="n1",
            kind="implement",
            final_state="VALIDATED",
            commit="a" * 40,
            routes=("openai/gpt-x",),
            verification=(VerificationResult(command="pytest -q", exit_code=0),),
        ),
        NodeSummary(
            node_id="n2",
            kind="implement",
            final_state="VALIDATED",
            commit=None,
            routes=(),
            verification=(VerificationResult(command="ruff check .", exit_code=1),),
        ),
    )


def _plan(**overrides: object) -> DeliverPlan:
    plan = DeliverPlan(
        run_id="run-1",
        goal="add feature x",
        issue_number=42,
        base_branch="main",
        head_branch="verdict/issue-42-feature",
        integration_commit="b" * 40,
        receipt_path=Path("/runs/run-1/receipt.json"),
        receipt_digest="sha256:" + "c" * 64,
        events_digest="sha256:" + "d" * 64,
        openspec_change="verdict-deliver",
        nodes=_nodes(),
        review=ReviewEvidence(
            status="PASS",
            reviewer="ocr",
            route_id="anthropic/claude-y",
            blocking=0,
            level="family",
            implementer_families=("openai",),
            reviewer_family="anthropic",
        ),
    )
    return replace(plan, **overrides) if overrides else plan


def test_render_is_byte_identical_for_same_plan() -> None:
    first = render_pull_request(_plan())
    second = render_pull_request(_plan())
    assert isinstance(first, RenderedPullRequest)
    assert first.title.encode() == second.title.encode()
    assert first.body.encode() == second.body.encode()


def test_body_carries_required_facts() -> None:
    body = render_pull_request(_plan()).body
    assert "add feature x" in body
    assert "`run-1`" in body
    assert "Refs #42" in body
    assert "verdict-deliver" in body
    assert "n1" in body and "n2" in body
    assert "implement" in body and "VALIDATED" in body
    assert "pytest -q" in body and "ruff check ." in body
    assert "PASS" in body and "anthropic/claude-y" in body
    assert "receipt.json" in body
    assert "sha256:" + "c" * 64 in body
    assert "sha256:" + "d" * 64 in body
    assert "draft" in body.lower() and "Verdict-generated" in body
    assert "/runs/" not in body


def test_body_has_no_closing_keyword() -> None:
    plan = _plan(goal="Fixes #1 and closes owner/repo#2, resolved: #3")
    body = render_pull_request(plan).body
    assert not _CLOSING.search(body)
    assert "Refs #42" in body


def test_title_is_single_line_and_bounded() -> None:
    plan = _plan(goal="x" * 500 + "\nline two\r\n")
    title = render_pull_request(plan).title
    assert "\n" not in title and "\r" not in title
    assert 0 < len(title) <= MAX_TITLE_LENGTH
    assert title.endswith("...")


def test_title_for_empty_goal_falls_back_to_run_id() -> None:
    title = render_pull_request(_plan(goal="  \n ")).title
    assert "run-1" in title


def test_mentions_html_and_markdown_are_neutralized() -> None:
    hostile = (
        "ping @octocat <script>alert(1)</script> ## Heading\n```\n- [x](http://e.x) **bold** | a"
    )
    plan = _plan(goal=hostile)
    rendered = render_pull_request(plan)
    assert not _MENTION.search(rendered.body)
    assert not _MENTION.search(rendered.title)
    assert "<script>" not in rendered.body
    assert "&lt;script&gt;" in rendered.body
    assert "```" not in rendered.body.replace("\\`\\`\\`", "")
    goal_lines = [line for line in rendered.body.splitlines() if "octocat" in line]
    assert len(goal_lines) == 1
    assert not goal_lines[0].startswith(("#", "-", "`", ">"))
    assert not any(line.startswith("## Heading") for line in rendered.body.splitlines())


def test_node_text_is_neutralized() -> None:
    node = replace(_nodes()[0], node_id="n1 @evil\n# x", final_state="<b>DONE</b>")
    plan = _plan(nodes=(node,))
    details = {
        node.node_id: NodeDetail(
            objective="Closes #9 by @team\n## injected <img src=x>",
            changed_files=("a.py", "b`c.py\n# x"),
        )
    }
    body = render_pull_request(plan, node_details=details).body
    assert not _MENTION.search(body)
    assert not _CLOSING.search(body)
    assert "<img" not in body and "<b>" not in body
    assert not any(line.startswith("## injected") for line in body.splitlines())
    assert "a.py" in body


def test_node_objective_and_changed_files_render() -> None:
    plan = _plan()
    details = {"n1": NodeDetail(objective="build the thing", changed_files=("pkg/mod.py",))}
    body = render_pull_request(plan, node_details=details).body
    assert "build the thing" in body
    assert "pkg/mod.py" in body


def test_long_goal_is_truncated_with_marker() -> None:
    body = render_pull_request(_plan(goal="g" * 100_000)).body
    assert TRUNCATION_MARKER in body
    assert len(body) <= MAX_BODY_LENGTH
    assert "sha256:" + "c" * 64 in body


def test_many_nodes_bound_body_and_keep_receipt() -> None:
    nodes = tuple(
        NodeSummary(
            node_id=f"node-{i}",
            kind="implement",
            final_state="VALIDATED",
            commit="e" * 40,
            routes=("r/a", "r/b"),
            verification=(VerificationResult(command="pytest " + "z" * 300, exit_code=0),),
        )
        for i in range(2000)
    )
    body = render_pull_request(_plan(nodes=nodes)).body
    assert len(body) <= MAX_BODY_LENGTH
    assert TRUNCATION_MARKER in body
    assert "sha256:" + "c" * 64 in body
    assert "sha256:" + "d" * 64 in body
    assert "Verdict-generated" in body


def test_short_plan_has_no_truncation_marker() -> None:
    rendered = render_pull_request(_plan())
    assert TRUNCATION_MARKER not in rendered.body
    assert TRUNCATION_MARKER not in rendered.title


def test_unknown_review_and_missing_openspec_render_explicitly() -> None:
    plan = _plan(
        openspec_change=None,
        review=ReviewEvidence(
            status="PASS",
            reviewer="ocr",
            route_id="",
            blocking=0,
            level="unknown",
            implementer_families=(),
            reviewer_family="",
        ),
        nodes=(),
    )
    body = render_pull_request(plan).body
    assert "none" in body.lower()
    assert "unknown" in body.lower()


def test_ansi_and_bidi_controls_are_stripped() -> None:
    plan = _plan(goal="a\x1b[31mred\x00\u202eevil\u200b")
    rendered = render_pull_request(plan)
    for text in (rendered.title, rendered.body):
        assert "\x1b" not in text and "\x00" not in text and "\u202e" not in text


def test_mention_after_a_backtick_is_neutralized() -> None:
    """A backtick in untrusted text is escaped, so it never shields the mention after it."""
    plan = _plan(goal="fix `@octocat and `@org/team now")
    rendered = render_pull_request(plan)
    assert not _MENTION.search(rendered.body)
    assert "`@octocat`" in rendered.body
    assert "`@org/team`" in rendered.body


def test_mention_check_can_fail_on_an_unwrapped_mention() -> None:
    assert _MENTION.search("hello @octocat")
    assert _MENTION.search("\\`@octocat")  # an escaped backtick opens no code span
    assert _MENTION.search("a `x` and @octocat")
    assert not _MENTION.search("a `@octocat` b")


@pytest.mark.parametrize(
    "goal",
    [
        "`@octocat",
        "``@octocat``",
        "x`@octocat`y",
        "(@octocat)",
        "-@octocat",
        "a.@octocat",
        "mail me@octocat.example",
        "word_@octocat",
        "Closes `#12 and `@octocat",
    ],
)
def test_no_mention_survives_whatever_precedes_it(goal: str) -> None:
    plan = _plan(goal=goal)
    node = replace(_nodes()[0], node_id=goal)
    body = render_pull_request(
        replace(plan, nodes=(node,)),
        node_details={node.node_id: NodeDetail(objective=goal, changed_files=(goal,))},
    ).body
    assert not _MENTION.search(body), body
    assert not _CLOSING.search(body)
