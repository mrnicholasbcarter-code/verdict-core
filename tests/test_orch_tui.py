from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from verdict.orchestration.contracts import NodeState, RunEvent
from verdict.orchestration.tui import RunView, event_line, follow, render_text


def event(seq: int, kind: str, node: str = "", **data: object) -> RunEvent:
    return RunEvent(
        seq=seq, at=f"2026-01-01T00:00:{seq:02d}+00:00", type=kind, node_id=node, data=data
    )


@pytest.fixture
def completed_events() -> list[RunEvent]:
    return [
        event(1, "run_started", goal="Ship [bold]feature[/bold]"),
        event(2, "plan_started", route_id="cc/planner"),
        event(
            3,
            "plan_ready",
            nodes=[{"node_id": "N1", "objective": "build"}],
            layers=[["N1"]],
            topology="SOLO",
            rationale=["bounded"],
        ),
        event(4, "eligibility", "N1", discovered=5, entitled=4, healthy=3, available=2, eligible=1),
        event(
            5,
            "selection",
            "N1",
            route_id="cc/claude-sonnet-5",
            provider="cc",
            capacity_class="subscription",
            rank=1,
            attempt=1,
        ),
        event(6, "dispatch", "N1", route_id="cc/claude-sonnet-5", attempt=1),
        event(7, "node_state", "N1", state="RUNNING"),
        event(
            8,
            "failure",
            "N1",
            category="quota_exhausted",
            action="REROUTE",
            route_id="cc/claude-sonnet-5",
            evidence="quota",
        ),
        event(9, "cooldown", "N1", key="cc", scope="provider", category="quota", until="later"),
        event(
            10,
            "reassign",
            "N1",
            from_route="cc/claude-sonnet-5",
            to_route="openai/gpt",
            reason="quota",
            attempt=2,
        ),
        event(11, "node_state", "N1", state="VALIDATED", route_id="openai/gpt", attempt=2),
        event(12, "barrier", name="integrate", ok=True, detail="clean"),
        event(13, "verify", "N1", ok=True, command="pytest", exit_code=0),
        event(
            14,
            "review",
            status="PASS",
            reviewer="reviewer",
            route_id="openai/gpt",
            blocking=0,
            findings=[],
        ),
        event(15, "controller", state="HEALTHY", detail="ready"),
        event(16, "integrate", ok=True, commits=["abc"]),
        event(17, "remediation", round=1),
        event(18, "terminal", "N1", ok=True, route_id="openai/gpt", duration_seconds=4),
        event(19, "run_finished", outcome="COMPLETE", reason="verified"),
    ]


def test_projection_and_complete_result(completed_events: list[RunEvent]) -> None:
    view = RunView.from_events(completed_events)
    assert view.goal == "Ship [bold]feature[/bold]"
    assert view.topology == "SOLO" and view.layers == [["N1"]]
    assert view.nodes["N1"].state is NodeState.VALIDATED
    assert view.nodes["N1"].route_id == "openai/gpt"
    assert view.nodes["N1"].attempt == 2
    assert view.nodes["N1"].history == [("openai/gpt", "ok")]
    assert view.eligibility["eligible"] == 1
    assert view.cooldowns["cc"].category == "quota"
    assert view.review is not None and view.review.status == "PASS"
    assert view.outcome == "COMPLETE" and view.reason == "verified"


def test_render_contains_all_stages_and_worker(completed_events: list[RunEvent]) -> None:
    text = render_text(completed_events)
    for label in (
        "GOAL",
        "PLAN",
        "DAG",
        "SELECT",
        "WORKERS",
        "QUOTA/COOLDOWN",
        "FAILURE/REASSIGN",
        "VERIFY",
        "REVIEW",
        "COMPLETE",
    ):
        assert label in text
    assert "openai/gpt" in text and "N1" in text and "PASS" in text


def test_plain_output_has_no_ansi(completed_events: list[RunEvent]) -> None:
    assert "\x1b" not in render_text(completed_events, plain=True)


def test_blocked_banner_and_reason() -> None:
    text = render_text([event(1, "run_finished", outcome="BLOCKED", reason="no route")])
    assert "BLOCKED" in text and "no route" in text


def test_json_mapping_uses_from_dict() -> None:
    view = RunView.from_events(
        [{"seq": 1, "at": "bad", "type": "run_started", "data": {"goal": "json"}}]
    )
    assert view.goal == "json"


def test_hostile_text_is_sanitized(completed_events: list[RunEvent]) -> None:
    view = RunView.from_events([event(1, "run_started", goal="x\x1b[31m red")])
    assert "\x1b" not in render_text([event(1, "run_started", goal="x\x1b[31m red")])
    assert view.goal == "x red"


def test_every_event_type_has_narration() -> None:
    types = [
        "run_started",
        "plan_started",
        "plan_ready",
        "topology",
        "eligibility",
        "node_state",
        "selection",
        "dispatch",
        "heartbeat",
        "terminal",
        "failure",
        "cooldown",
        "reassign",
        "verify",
        "barrier",
        "integrate",
        "review",
        "remediation",
        "controller",
        "run_finished",
    ]
    for seq, kind in enumerate(types, 1):
        line = event_line(
            event(
                seq,
                kind,
                "N1",
                goal="goal",
                state="HEALTHY",
                route_id="cc/model",
                to_route="openai/gpt",
                outcome="COMPLETE",
            )
        )
        assert line.startswith("[") and "]" in line and len(line) > 5, kind


def test_narrative_includes_selection_details() -> None:
    line = event_line(
        event(
            1,
            "selection",
            "N1",
            route_id="cc/claude-sonnet-5",
            capacity_class="subscription",
            rank=1,
        )
    )
    assert "[SELECT]" in line and "N1 -> cc/claude-sonnet-5" in line and "rank 1" in line


def test_reassign_updates_route_and_history() -> None:
    view = RunView.from_events(
        [
            event(1, "selection", "N", route_id="a/x", attempt=1),
            event(2, "terminal", "N", ok=False),
            event(3, "reassign", "N", to_route="b/y", attempt=2),
        ]
    )
    assert view.nodes["N"].route_id == "b/y"
    assert view.nodes["N"].history == [("a/x", "failed")]
    assert view.nodes["N"].glyph_key() == "reassigned"


def test_running_worker_elapsed_and_glyph() -> None:
    view = RunView.from_events(
        [
            event(1, "dispatch", "N", route_id="p/model", attempt=3),
            event(2, "node_state", "N", state="RUNNING"),
        ]
    )
    assert view.nodes["N"].state is NodeState.RUNNING
    assert view.nodes["N"].attempt == 3
    assert view.nodes["N"].glyph_key() == "running"


def test_follow_plain_prints_new_events(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"seq":1,"at":"bad","type":"run_started","data":{"goal":"hi"}}\n', encoding="utf-8"
    )
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=80)
    result = follow(path, console=console, stop_when_final=False, max_polls=1)
    assert result.goal == "hi" and "[GOAL] hi" in output.getvalue()


def test_invalid_jsonl_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("not json\n{}\n", encoding="utf-8")
    assert follow(path, stop_when_final=False, max_polls=1).event_count == 0


def test_check_results_are_projected() -> None:
    view = RunView.from_events(
        [
            event(1, "barrier", name="gate", ok=False),
            event(2, "verify", ok=False, command="test", exit_code=1),
            event(3, "integrate", ok=True, commits=["a", "b"]),
        ]
    )
    assert view.barriers[0].ok is False and view.verifications[0].detail.endswith("(exit 1)")
    assert view.integrations[0].detail == "2 commit(s)"


def test_verify_lines_truncate_so_pass_fail_prefix_never_wraps() -> None:
    """A PASS command whose detail contains 'FAIL' must not produce a continuation
    line starting with 'FAIL'.  Each check line is truncated with '…' to fit the
    VERIFY panel's inner column width so word-wrap never starts with a status word.
    """
    from verdict.orchestration.tui import render

    # Craft a verify command whose detail contains 'FAIL' past the wrap point:
    # "PASS node-2 sh -c test -f node-2.txt && ! grep -q FAIL node-2.txt (exit 0)"
    # That is 72 chars - longer than the 51-char panel inner width.
    long_cmd = "sh -c test -f node-2.txt && ! grep -q FAIL node-2.txt"
    view = RunView.from_events(
        [event(1, "verify", "node-2", ok=True, command=long_cmd, exit_code=0)]
    )
    import io

    from rich.console import Console

    buf = io.StringIO()
    console = Console(file=buf, width=110, color_system=None, force_terminal=False)
    console.print(render(view, width=110, plain=True))
    text = buf.getvalue()

    lines = text.splitlines()
    # Extract the VERIFY section: lines between the VERIFY header and the next
    # section header (a line that is all-caps and ends without trailing data,
    # or starts with a title-case section name).
    in_verify = False
    verify_content: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        if stripped == "VERIFY":
            in_verify = True
            continue
        if in_verify:
            # A new top-level section header ends the VERIFY block
            if stripped in (
                "REVIEW",
                "COMPLETE",
                "BLOCKED",
                "WORKERS",
                "QUOTA/COOLDOWN",
                "FAILURE/REASSIGN",
                "SELECT",
                "HYDRATE",
                "GOAL",
                "UNDERSTAND",
                "CONTROLLER",
                "PLAN / DAG",
            ):
                break
            verify_content.append(ln)

    # No content line in the VERIFY block may start with "FAIL" for a passing check.
    # All verifications here are ok=True so no FAIL-prefix line should appear.
    fail_rows = [ln for ln in verify_content if ln.lstrip().startswith("FAIL")]
    assert not fail_rows, (
        f"VERIFY block has content line(s) starting with FAIL for a passing check: {fail_rows}"
    )
    # The PASS line must be present and must fit in 51 chars (inner panel width).
    pass_rows = [ln for ln in verify_content if ln.lstrip().startswith("PASS")]
    assert pass_rows, f"no PASS line found in VERIFY section; content={verify_content}"
    for row in pass_rows:
        assert len(row.strip()) <= 51, f"PASS line exceeds 51 chars: {row.strip()!r}"


def test_follow_skips_previous_controller_life(tmp_path) -> None:
    import json as _json

    from rich.console import Console

    from verdict.orchestration.tui import follow

    path = tmp_path / "events.jsonl"
    rows = [
        {
            "seq": 1,
            "at": "2026-09-24T00:00:00Z",
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "BLOCKED", "reason": "old life"},
        },
        {
            "seq": 2,
            "at": "2026-09-24T00:01:00Z",
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "g"},
        },
        {
            "seq": 3,
            "at": "2026-09-24T00:02:00Z",
            "type": "run_finished",
            "node_id": "",
            "data": {"outcome": "COMPLETE", "reason": "new life"},
        },
    ]
    path.write_text("\n".join(_json.dumps(r) for r in rows) + "\n")
    console = Console(file=__import__("io").StringIO(), force_terminal=False, width=100)
    view = follow(path, console=console, start_seq=1, poll_seconds=0, max_polls=3)
    text = console.file.getvalue()
    assert "old life" not in text and "new life" in text
    assert view.final


def test_wide_layout_shows_controller_history_and_independence() -> None:
    from verdict.orchestration.tui import render_text

    events = [
        {
            "seq": 1,
            "at": "2026-09-24T00:00:00Z",
            "type": "run_started",
            "node_id": "",
            "data": {"goal": "g"},
        },
        {
            "seq": 2,
            "at": "2026-09-24T00:00:01Z",
            "type": "plan_started",
            "node_id": "",
            "data": {"route_id": "cc/claude-fable-5"},
        },
        {
            "seq": 3,
            "at": "2026-09-24T00:00:02Z",
            "type": "controller",
            "node_id": "",
            "data": {"state": "QUOTA", "route_id": "cc/claude-fable-5", "detail": "usage limit"},
        },
        {
            "seq": 4,
            "at": "2026-09-24T00:00:03Z",
            "type": "dispatch",
            "node_id": "a",
            "data": {"route_id": "cc/claude-sonnet-5", "attempt": 1},
        },
        {
            "seq": 5,
            "at": "2026-09-24T00:00:04Z",
            "type": "terminal",
            "node_id": "a",
            "data": {"ok": False, "route_id": "cc/claude-sonnet-5"},
        },
        {
            "seq": 6,
            "at": "2026-09-24T00:00:05Z",
            "type": "failure",
            "node_id": "a",
            "data": {
                "category": "quota_exhausted",
                "action": "REROUTE",
                "route_id": "cc/claude-sonnet-5",
                "fault_injected": True,
            },
        },
        {
            "seq": 7,
            "at": "2026-09-24T00:00:06Z",
            "type": "terminal",
            "node_id": "a",
            "data": {"ok": True, "route_id": "cx/gpt-5.5"},
        },
        {
            "seq": 8,
            "at": "2026-09-24T00:00:07Z",
            "type": "controller",
            "node_id": "",
            "data": {
                "state": "REVIEW_INDEPENDENCE",
                "level": "family",
                "excluded_routes": ["cx/gpt-5.5"],
                "reviewer_route": "cc/claude-opus-4-8",
            },
        },
        {
            "seq": 9,
            "at": "2026-09-24T00:00:08Z",
            "type": "review",
            "node_id": "",
            "data": {"status": "PASS", "reviewer": "ocr", "route_id": "cc/claude-opus-4-8"},
        },
    ]
    import re as _re

    text = _re.sub(r"\x1b\[[0-9;]*m", "", render_text(events, width=140, plain=False))
    assert "VERDICT" in text and "CONTROLLER" in text
    assert "QUOTA" in text and "[injected]" in text
    assert "sonnet-5" in text and "gpt-5.5" in text
    assert "excluded implementers: cx/gpt-5.5" in text


def test_reviewer_failover_and_context_recovery_are_visible() -> None:
    """BOD-267: reviewer attempts, repack and rehydrate events render in the view."""
    from verdict.orchestration.tui import RunView, render_text

    events = [
        {"seq": 1, "at": "2026-09-27T20:00:00Z", "type": "repack", "node_id": "a",
         "data": {"from_budget_bytes": 60000, "to_budget_bytes": 30000}},
        {"seq": 2, "at": "2026-09-27T20:00:01Z", "type": "rehydrate", "node_id": "a",
         "data": {"route_id": "kr/claude-haiku-4.5"}},
        {"seq": 3, "at": "2026-09-27T20:00:02Z", "type": "review_attempt", "node_id": "",
         "data": {"route_id": "cc/stuck", "status": "ERROR", "category": "timeout"}},
        {"seq": 4, "at": "2026-09-27T20:00:03Z", "type": "review_attempt", "node_id": "",
         "data": {"route_id": "cx/gpt-5.4", "status": "PASS", "category": ""}},
        {"seq": 5, "at": "2026-09-27T20:00:04Z", "type": "review", "node_id": "",
         "data": {"status": "PASS", "reviewer": "ocr", "route_id": "cx/gpt-5.4"}},
    ]  # fmt: skip
    view = RunView.from_events(events)
    assert view.review_attempts == [("cc/stuck", "ERROR", "timeout"), ("cx/gpt-5.4", "PASS", "")]
    text = render_text(events, width=140, plain=True)
    assert "cc/stuck[ERROR:timeout] -> cx/gpt-5.4[PASS]" in text
    assert "context 60000 -> 30000 bytes after overflow" in text
    assert "retry on kr/claude-haiku-4.5 with verification evidence" in text


def test_candidate_panel_follows_each_route_through_failover() -> None:
    """BOD-267: the recorded live run shows failed route -> cooldown -> replacement."""
    import json
    from pathlib import Path

    from verdict.orchestration.tui import RunView, render_text

    path = Path(__file__).resolve().parent.parent / "docs/proof/live-smoke-run/events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    view = RunView.from_events(events)
    assert view.candidates == {
        "kr/claude-haiku-4.5": ["selected", "failed:upstream_temporary", "cooled:route"],
        "kr/claude-sonnet-4": ["selected", "ok"],
    }
    text = render_text(events, width=140, plain=True)
    assert "kr/claude-haiku-4.5  selected > failed:upstream_temporary > cooled:route" in text


def test_candidate_panel_marks_revoked_and_provider_cooldown() -> None:
    from verdict.orchestration.tui import RunView

    events = [
        {"seq": 1, "at": "2026-09-27T21:00:00Z", "type": "selection", "node_id": "a",
         "data": {"route_id": "cc/a"}},
        {"seq": 2, "at": "2026-09-27T21:00:01Z", "type": "eligibility", "node_id": "a",
         "data": {"revoked": "cc/a", "reason": "pre-dispatch recheck"}},
        {"seq": 3, "at": "2026-09-27T21:00:02Z", "type": "selection", "node_id": "a",
         "data": {"route_id": "cc/b"}},
        {"seq": 4, "at": "2026-09-27T21:00:03Z", "type": "failure", "node_id": "a",
         "data": {"route_id": "cc/b", "category": "rate_limited"}},
        {"seq": 5, "at": "2026-09-27T21:00:04Z", "type": "cooldown", "node_id": "a",
         "data": {"key": "cc", "scope": "provider", "category": "rate_limited"}},
    ]  # fmt: skip
    view = RunView.from_events(events)
    assert view.candidates["cc/a"] == ["selected", "revoked", "cooled:provider"]
    assert view.candidates["cc/b"] == ["selected", "failed:rate_limited", "cooled:provider"]


def test_header_names_root_controller_and_failover_owner() -> None:
    """BOD-267: supervised runs name the root model; bare launches say root-pinned."""
    from verdict.orchestration.tui import render_text

    supervised = [
        {"seq": 1, "at": "2026-09-27T21:00:00Z", "type": "run_started", "node_id": "",
         "data": {"goal": "g", "controller_route": "omniroute/kr/claude-opus-5.5",
                  "controller_generation": "1"}},
    ]  # fmt: skip
    text = render_text(supervised, width=140, plain=True)
    assert "root controller: kr/claude-opus-5.5 (supervisor-owned failover, generation 1)" in text

    bare = [
        {"seq": 1, "at": "2026-09-27T21:00:00Z", "type": "run_started", "node_id": "",
         "data": {"goal": "g"}},
    ]  # fmt: skip
    text = render_text(bare, width=140, plain=True)
    assert "bare launch: root-pinned, no automatic failover" in text
