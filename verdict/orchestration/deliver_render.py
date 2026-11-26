"""Deterministic draft pull request title and body for ``verdict deliver``.

``render_pull_request`` is a pure function of a ``DeliverPlan`` (plus optional
per-node detail): it reads no clock, no environment and no files, so rendering
the same plan twice yields byte-identical output.

The goal, node ids, objectives, file names and receipt strings are untrusted.
Free text is rendered inline on a single line, with control characters removed,
whitespace collapsed, HTML and Markdown control characters escaped, and
``@mentions``, issue references and URLs wrapped in code spans so they cannot
notify anyone or act as a closing keyword. The only issue reference Verdict
writes itself is ``Refs #N``. Every field is length-bounded, and the whole body
is bounded by ``MAX_BODY_LENGTH`` with an explicit ``TRUNCATION_MARKER`` where
content was cut. See the ``verdict-deliver`` OpenSpec change.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from verdict.orchestration.deliver import DeliverPlan, NodeSummary

MAX_TITLE_LENGTH = 80
MAX_BODY_LENGTH = 30_000
TRUNCATION_MARKER = "[truncated]"

TITLE_PREFIX = "verdict: "
_TITLE_ELLIPSIS = "..."

_GOAL_LIMIT = 2_000
_OBJECTIVE_LIMIT = 300
_NODE_ID_LIMIT = 80
_LABEL_LIMIT = 40
_CODE_LIMIT = 200
_PATH_LIMIT = 120
_MAX_FILES = 10
_MAX_ROUTES = 5
_MAX_VERIFICATIONS = 5
_MAX_FAMILIES = 10
_OMITTED_RESERVE = 160

# A mention is wrapped wherever it appears: nothing before it (a backtick, a letter, a dot)
# makes it inert, because the text is untrusted and its backticks are escaped, never code
# delimiters. Issue references and URLs only need the word-boundary guard.
_TOKEN = re.compile(
    r"@[A-Za-z0-9_][\w./-]*|(?<![\w])(?:(?:[\w.-]+/[\w.-]+)?#\d+|https?://[^\s`]+)", re.IGNORECASE
)
_MD_ESCAPE = frozenset("\\`*_[]()|#~!")
_HTML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


@dataclass(frozen=True)
class NodeDetail:
    """Optional per-node facts the plan does not carry. Untrusted text."""

    objective: str = ""
    changed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderedPullRequest:
    title: str
    body: str


def _clean(text: str) -> str:
    """Drop control and format characters, turn whitespace into spaces, collapse runs."""
    kept: list[str] = []
    for ch in str(text):
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            continue
        else:
            kept.append(ch)
    return " ".join("".join(kept).split())


def _escape_char(text: str, index: int, ch: str) -> str:
    if ch in _HTML_ESCAPE:
        return _HTML_ESCAPE[ch]
    if ch in _MD_ESCAPE:
        return "\\" + ch
    if index == 0 and ch in "-+=":
        return "\\" + ch
    if ch == "." and index > 0 and text[:index].isdigit():
        return "\\" + ch
    return ch


def _pieces(text: str) -> Iterator[str]:
    """Atomic output pieces, so truncation never splits an escape or a code span."""
    pos = 0
    for match in _TOKEN.finditer(text):
        for i in range(pos, match.start()):
            yield _escape_char(text, i, text[i])
        yield "`" + match.group().replace("`", "'") + "`"
        pos = match.end()
    for i in range(pos, len(text)):
        yield _escape_char(text, i, text[i])


def _inline(text: str, limit: int) -> str:
    """Neutralized single-line text of at most ``limit`` characters."""
    pieces = list(_pieces(_clean(text)))
    if sum(len(p) for p in pieces) <= limit:
        return "".join(pieces)
    budget = limit - len(TRUNCATION_MARKER) - 1
    out: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > budget:
            break
        out.append(piece)
        used += len(piece)
    return "".join(out) + " " + TRUNCATION_MARKER


def _code(text: str, limit: int = _CODE_LIMIT) -> str:
    """A bounded inline code span; untrusted text cannot close it."""
    clean = _clean(text).replace("`", "'")
    if not clean:
        return "(none)"
    if len(clean) > limit:
        clean = clean[: limit - len(TRUNCATION_MARKER) - 1].rstrip() + " " + TRUNCATION_MARKER
    return "`" + clean + "`"


def _title_text(text: str) -> str:
    return _clean(text).replace("@", "").replace("#", "")


def _render_title(plan: DeliverPlan) -> str:
    first_line = next((ln for ln in str(plan.goal).splitlines() if _clean(ln)), "")
    subject = _title_text(first_line) or f"run {_title_text(plan.run_id)}"
    room = MAX_TITLE_LENGTH - len(TITLE_PREFIX)
    if len(subject) > room:
        subject = subject[: room - len(_TITLE_ELLIPSIS)].rstrip() + _TITLE_ELLIPSIS
    return TITLE_PREFIX + subject


def _verification_line(plan: DeliverPlan) -> str:
    results = [v for n in plan.nodes for v in n.verification]
    passed = sum(1 for v in results if v.exit_code == 0)
    return f"- Verification: {passed} of {len(results)} command(s) exited 0"


def _node_block(node: NodeSummary, detail: NodeDetail | None) -> str:
    lines = [
        f"- **{_inline(node.node_id, _NODE_ID_LIMIT) or '(unnamed)'}**: "
        f"{_inline(node.kind, _LABEL_LIMIT)}, status {_inline(node.final_state, _LABEL_LIMIT)}"
    ]
    if detail is not None and _clean(detail.objective):
        lines.append(f"  - Objective: {_inline(detail.objective, _OBJECTIVE_LIMIT)}")
    if node.commit:
        lines.append(f"  - Commit: {_code(node.commit)}")
    if node.routes:
        shown = ", ".join(_code(r) for r in node.routes[:_MAX_ROUTES])
        extra = len(node.routes) - _MAX_ROUTES
        lines.append(f"  - Routes: {shown}" + (f" and {extra} more" if extra > 0 else ""))
    if detail is not None and detail.changed_files:
        shown = ", ".join(_code(f, _PATH_LIMIT) for f in detail.changed_files[:_MAX_FILES])
        extra = len(detail.changed_files) - _MAX_FILES
        suffix = f" and {extra} more {TRUNCATION_MARKER}" if extra > 0 else ""
        lines.append(f"  - Changed files: {shown}{suffix}")
    for result in node.verification[:_MAX_VERIFICATIONS]:
        code = "no exit code" if result.exit_code is None else f"exit {result.exit_code}"
        lines.append(f"  - Verification: {_code(result.command)} ({code})")
    extra_checks = len(node.verification) - _MAX_VERIFICATIONS
    if extra_checks > 0:
        lines.append(f"  - {extra_checks} more verification command(s) {TRUNCATION_MARKER}")
    return "\n".join(lines)


def _review_lines(plan: DeliverPlan) -> list[str]:
    review = plan.review
    families = ", ".join(_code(f) for f in review.implementer_families[:_MAX_FAMILIES])
    return [
        f"- Review gate: {_inline(review.status, _LABEL_LIMIT)} "
        f"({review.blocking} blocking finding(s))",
        f"- Independence level: {_inline(review.level, _LABEL_LIMIT)}",
        f"- Reviewer: {_code(review.reviewer)}, route {_code(review.route_id)}, "
        f"family {_code(review.reviewer_family)}",
        f"- Implementer families: {families or '(none)'}",
    ]


def render_pull_request(
    plan: DeliverPlan, node_details: Mapping[str, NodeDetail] | None = None
) -> RenderedPullRequest:
    """Render the draft pull request title and body for ``plan``. Pure and deterministic."""
    details = node_details or {}
    head = [
        "> **Verdict-generated draft.** This pull request was opened as a draft by "
        "`verdict deliver` from a verified, complete run. Verdict marks it ready only after "
        "a cross-family review and green checks on the exact head commit. It never merges, "
        "approves or closes issues.",
        "",
        f"Refs #{plan.issue_number}",
        "",
        "## Goal",
        "",
        _inline(plan.goal, _GOAL_LIMIT) or "(none)",
        "",
        "## Run",
        "",
        f"- Run id: {_code(plan.run_id)}",
        f"- OpenSpec change: {_code(plan.openspec_change or '')}",
        f"- Branch: {_code(plan.head_branch)} into {_code(plan.base_branch)}",
        f"- Integration commit: {_code(plan.integration_commit)}",
    ]
    tail = [
        "",
        "## Verification and review",
        "",
        _verification_line(plan),
        *_review_lines(plan),
        "",
        "## Receipt",
        "",
        f"- Receipt: {_code(plan.receipt_path.name)} of run {_code(plan.run_id)}",
        f"- Receipt digest: {_code(plan.receipt_digest)}",
        f"- Events digest: {_code(plan.events_digest)}",
        "",
        "_Generated by Verdict from the recorded run. Verify the receipt before trusting it._",
    ]
    nodes_head = ["", "## Nodes", ""]

    fixed = "\n".join([*head, *nodes_head, *tail])
    budget = MAX_BODY_LENGTH - len(fixed) - _OMITTED_RESERVE
    blocks: list[str] = []
    omitted = 0
    for index, node in enumerate(plan.nodes):
        block = _node_block(node, details.get(node.node_id))
        if len(block) + 1 > budget:
            omitted = len(plan.nodes) - index
            break
        blocks.append(block)
        budget -= len(block) + 1
    if not plan.nodes:
        blocks.append("No nodes recorded.")
    if omitted:
        blocks.append(f"- {omitted} more node(s) omitted {TRUNCATION_MARKER}")

    body = "\n".join([*head, *nodes_head, *blocks, *tail]) + "\n"
    if len(body) > MAX_BODY_LENGTH:
        body = body[: MAX_BODY_LENGTH - len(TRUNCATION_MARKER) - 1] + "\n" + TRUNCATION_MARKER
    return RenderedPullRequest(title=_render_title(plan), body=body)
