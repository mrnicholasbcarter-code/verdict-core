"""Calibration labels come from verified outcomes (OJ1, #1220).

Each run is written as a real event log and turned into a real receipt, so the
labels are read from the shape the runtime produces, not from a hand-made dict.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.decision_signals.calibration import CalibrationRecord, evaluate, load_records
from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.receipt import EventLog, build_run_receipt

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_calibration_records.py"
_spec = importlib.util.spec_from_file_location("build_calibration_records_oj1", _SCRIPT)
assert _spec and _spec.loader
_builder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_builder)

FIXED = datetime(2026, 11, 28, 1, 2, 3, tzinfo=timezone.utc)
VERIFY = ("pytest", "-q", "tests/test_a.py")
PASS = {"status": "PASS", "reviewer": "ocr v1", "route_id": "gm/gemini-3", "findings": []}


def _clock() -> datetime:
    return FIXED


def _new_run(tmp_path: Path, *, second_worker: bool = False) -> tuple[Path, EventLog]:
    graph = WorkGraph(
        goal="add feature x",
        nodes=(
            WorkNode("a", "build a", owned_files=("pkg/a.py",), verification_command=VERIFY,
                     barrier="int"),
            *(
                [WorkNode("b", "build b", owned_files=("pkg/b.py",), verification_command=VERIFY)]
                if second_worker
                else []
            ),
            WorkNode("merge", "integrate a", kind="integrate", depends_on=("a",)),
            WorkNode("rev", "review", kind="review", depends_on=("merge",)),
        ),
    )  # fmt: skip
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl", clock=_clock)
    log.emit("run_started", run_id="run-1", goal="add feature x")
    return run_dir, log


def _attempt(
    log: EventLog,
    number: int,
    route: str,
    capacity: str,
    tier: int | None,
    *,
    verified: bool,
    category: str | None = None,
    fault_injected: bool = False,
    exit_code: int = 0,
) -> None:
    extra: dict[str, Any] = {} if tier is None else {"capability_tier": tier}
    if fault_injected:
        extra["fault_injected"] = True
    log.emit(
        "dispatch", node_id="a", attempt=number, route_id=route, capacity_class=capacity, **extra
    )
    log.emit("terminal", node_id="a", attempt=number, ok=True, duration_seconds=1.0)
    log.emit("verify", node_id="a", ok=verified, command=list(VERIFY), exit_code=exit_code)
    if category is not None:
        log.emit("failure", node_id="a", attempt=number, category=category)


def _write_receipt(run_dir: Path) -> None:
    (run_dir / "receipt.json").write_text(json.dumps(build_run_receipt(run_dir)))


def _finish(run_dir: Path, log: EventLog, *, complete: bool) -> Path:
    if complete:
        log.emit("barrier", node_id="merge", name="int", ok=True)
        log.emit("integrate", node_id="merge", commit="abc123")
        log.emit("terminal", node_id="merge", attempt=1, ok=True, route_id="local/git")
        log.emit("verify", node_id="merge", ok=True, command=["pytest"], exit_code=0)
        (run_dir / "review.json").write_text(json.dumps(PASS))
    log.emit("run_finished", outcome="COMPLETE" if complete else "BLOCKED", reason="done")
    _write_receipt(run_dir)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    assert receipt["outcome"] == ("COMPLETE" if complete else "BLOCKED"), receipt["reason"]
    return run_dir


def _record(run_dir: Path) -> dict[str, Any]:
    """The record the builder writes for this run: receipt plus its event log."""
    records, skipped = _builder.process_runs_dir(run_dir.parent)
    assert len(records) == 1, skipped
    return records[0]


# ------------------------------------------------------------------ outcome, class, tier, attempts


def test_accepted_on_second_attempt_on_a_subscription_route(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=False, category="verification_failed",
             exit_code=1)  # fmt: skip
    _attempt(log, 2, "cc/claude-sonnet-5", "subscription", 2, verified=True)

    record = _record(_finish(run_dir, log, complete=True))

    assert record["outcome"] == "accepted"
    assert record["excluded_reason"] is None
    assert record["capacity_class"] == "subscription"
    assert record["capability_tier"] == 2
    assert record["attempts"] == 2
    assert record["verified"] is True
    loaded = CalibrationRecord.from_dict(record)
    assert (loaded.outcome, loaded.capacity_class, loaded.attempts) == (
        "accepted",
        "subscription",
        2,
    )


def test_a_run_that_failed_the_work_is_a_task_failure(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=False, category="verification_failed",
             exit_code=1)  # fmt: skip

    record = _record(_finish(run_dir, log, complete=False))

    assert record["outcome"] == "not_accepted"
    assert record["excluded_reason"] is None
    assert record["attempts"] == 1
    assert record["capacity_class"] is None  # nothing passed


@pytest.mark.parametrize(
    "category", ["environment_error", "model_mismatch", "route_denied", "quota_exhausted"]
)
def test_an_environment_or_infrastructure_failure_gets_no_task_label(
    tmp_path: Path, category: str
) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=False, category=category, exit_code=127)

    record = _record(_finish(run_dir, log, complete=False))

    assert record["outcome"] is None
    assert record["excluded_reason"] == category
    assert record["attempts"] == 0  # the work was never judged
    loaded = CalibrationRecord.from_dict(record)
    assert loaded.outcome is None and loaded.excluded_reason == category


@pytest.mark.parametrize("route", ["", "gh/gpt-5"])  # "" is what "no eligible model" emits
def test_pool_exhaustion_before_any_dispatch_gets_no_task_label(tmp_path: Path, route: str) -> None:
    run_dir, log = _new_run(tmp_path)
    log.emit("failure", node_id="a", category="pool_exhausted", action="FAIL_CLOSED",
             route_id=route)  # fmt: skip

    record = _record(_finish(run_dir, log, complete=False))

    assert record["outcome"] is None
    assert record["excluded_reason"] == "pool_exhausted"
    assert record["attempts"] == 0


def test_a_node_that_never_ran_excludes_the_run_even_when_another_node_passed(
    tmp_path: Path,
) -> None:
    run_dir, log = _new_run(tmp_path, second_worker=True)
    _attempt(log, 1, "cc/claude-sonnet-5", "subscription", 2, verified=True)
    log.emit("failure", node_id="b", category="pool_exhausted", action="FAIL_CLOSED",
             route_id="")  # fmt: skip

    record = _record(_finish(run_dir, log, complete=False))

    assert record["outcome"] is None
    assert record["excluded_reason"] == "pool_exhausted"
    assert record["attempts"] == 1  # node a's judged attempt
    assert record["capacity_class"] == "subscription"  # the route that did pass


def test_an_exhausted_budget_after_judged_failures_is_still_a_task_failure(tmp_path: Path) -> None:
    """The node-level pool_exhausted failure overwrites the last receipt row's category."""
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=False, category="verification_failed",
             exit_code=1)  # fmt: skip
    _attempt(log, 2, "cc/claude-sonnet-5", "subscription", 2, verified=False,
             category="verification_failed", exit_code=1)  # fmt: skip
    log.emit("failure", node_id="a", category="pool_exhausted", action="FAIL_CLOSED",
             route_id="cc/claude-sonnet-5", evidence="replacement budget exhausted")  # fmt: skip
    _finish(run_dir, log, complete=False)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    row = receipt["nodes"][0]["attempts"][-1]
    assert row["failure_category"] == "pool_exhausted"  # the receipt row lost the real class

    record = _record(run_dir)

    assert record["outcome"] == "not_accepted"
    assert record["excluded_reason"] is None
    assert record["attempts"] == 2


def test_a_task_failure_before_an_infrastructure_failure_is_still_a_task_failure(
    tmp_path: Path,
) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=False, category="verification_failed",
             exit_code=1)  # fmt: skip
    _attempt(log, 2, "cc/claude-sonnet-5", "subscription", 2, verified=False,
             category="quota_exhausted")  # fmt: skip

    record = _record(_finish(run_dir, log, complete=False))

    assert record["outcome"] == "not_accepted"
    assert record["excluded_reason"] is None
    assert record["attempts"] == 1  # only the first attempt's work was judged


def test_an_infrastructure_failure_does_not_hide_a_task_failure(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=False, category="model_mismatch")
    _attempt(log, 2, "cc/claude-sonnet-5", "subscription", 2, verified=False,
             category="verification_failed", exit_code=1)  # fmt: skip

    record = _record(_finish(run_dir, log, complete=False))

    assert record["outcome"] == "not_accepted"
    assert record["attempts"] == 1  # the mismatched attempt did no judged work


def test_a_cancelled_run_gets_no_task_label(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=True)
    log.emit("run_finished", outcome="CANCELLED", reason="operator")
    _write_receipt(run_dir)
    record = _record(run_dir)
    assert record["outcome"] is None
    assert record["excluded_reason"] == "cancelled"


def test_the_topology_label_is_gone(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "cc/claude-sonnet-5", "subscription", 2, verified=True)
    record = _record(_finish(run_dir, log, complete=True))
    assert "frontier_needed" not in record
    assert "frontier_needed" not in CalibrationRecord.__dataclass_fields__


def test_a_fault_injected_run_makes_no_record(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=True, fault_injected=True)
    _finish(run_dir, log, complete=True)
    records, skipped = _builder.process_runs_dir(run_dir.parent)
    assert records == []
    assert skipped == {"fault-injected run": 1}


# ------------------------------------------------------------------ lifts


def test_lifts_the_events_show_are_recorded_and_the_rest_stay_unknown(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    log.emit("dispatch", node_id="a", attempt=1, route_id="gh/gpt-5", capacity_class="free",
             capability_tier=3)  # fmt: skip
    log.emit("hydrate", node_id="a", context_bytes=2048, prompt_tokens_estimated=512,
             sources=["pkg/a.py"], truncated=False)  # fmt: skip
    log.emit("terminal", node_id="a", attempt=1, ok=True, duration_seconds=1.0)
    log.emit("lint_autofix", node_id="a", files=["pkg/a.py"], changed_files=["pkg/a.py"],
             skipped=False, ruff_missing=False)  # fmt: skip
    log.emit("verify", node_id="a", ok=True, command=list(VERIFY), exit_code=0)

    lifts = _record(_finish(run_dir, log, complete=True))["lifts"]

    assert lifts == {"context_pack": True, "template": None, "lint_autofix": True,
                     "node_split": None}  # fmt: skip


def test_a_run_whose_events_say_nothing_about_a_lift_leaves_it_unknown(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", 3, verified=True)

    lifts = _record(_finish(run_dir, log, complete=True))["lifts"]

    assert lifts["context_pack"] is None
    assert lifts["lint_autofix"] is None


def test_an_empty_pack_and_an_autofix_that_changed_nothing_are_recorded_absent(
    tmp_path: Path,
) -> None:
    run_dir, log = _new_run(tmp_path)
    log.emit("dispatch", node_id="a", attempt=1, route_id="gh/gpt-5", capacity_class="free")
    log.emit("hydrate", node_id="a", context_bytes=0, sources=[], truncated=False)
    log.emit("terminal", node_id="a", attempt=1, ok=True, duration_seconds=1.0)
    log.emit("lint_autofix", node_id="a", files=["pkg/a.py"], changed_files=[],
             skipped=False, ruff_missing=False)  # fmt: skip
    log.emit("verify", node_id="a", ok=True, command=list(VERIFY), exit_code=0)

    lifts = _record(_finish(run_dir, log, complete=True))["lifts"]

    assert lifts["context_pack"] is False
    assert lifts["lint_autofix"] is False


# ------------------------------------------------------------------ the receipt carries the tier


def test_the_receipt_projects_the_tier_only_when_the_dispatch_recorded_one(tmp_path: Path) -> None:
    run_dir, log = _new_run(tmp_path)
    _attempt(log, 1, "gh/gpt-5", "free", None, verified=False, category="verification_failed",
             exit_code=1)  # fmt: skip
    _attempt(log, 2, "cc/claude-sonnet-5", "subscription", 2, verified=True)

    _finish(run_dir, log, complete=True)
    receipt = json.loads((run_dir / "receipt.json").read_text())
    rows = {n["node_id"]: n for n in receipt["nodes"]}["a"]["attempts"]

    assert "capability_tier" not in rows[0]
    assert rows[1]["capability_tier"] == 2


# ------------------------------------------------------------------ old records, the report


def test_records_written_before_the_labels_still_load(tmp_path: Path) -> None:
    legacy = {
        "task_id": "t1", "task_class": "debugging", "frontier_worthy": 0.4, "confidence": 0.8,
        "security_sensitive": 0.1, "frontier_needed": True, "security_relevant": False,
        "planner_was_frontier": False, "verified": True, "first_pass": True,
    }  # fmt: skip
    path = tmp_path / "old.jsonl"
    path.write_text(json.dumps(legacy) + "\n")

    (record,) = load_records(path)

    assert record.task_id == "t1" and record.verified is True
    assert record.outcome is None and record.excluded_reason is None
    assert record.capacity_class is None and record.capability_tier is None
    assert record.attempts is None and record.lifts is None
    assert evaluate([record]).records == 1


def _rec(task_id: str, **fields: Any) -> CalibrationRecord:
    base: dict[str, Any] = {
        "task_id": task_id, "task_class": "bounded_implementation", "frontier_worthy": 0.3,
        "confidence": 0.9, "security_sensitive": 0.1, "security_relevant": False,
        "planner_was_frontier": False, "verified": False, "first_pass": False,
    }  # fmt: skip
    return CalibrationRecord(**{**base, **fields})


def test_the_report_does_not_count_an_excluded_run_as_a_task_failure() -> None:
    lifts = {"context_pack": True, "template": None, "lint_autofix": False, "node_split": None}
    records = [
        _rec("ok", outcome="accepted", verified=True, first_pass=True, attempts=1,
             capacity_class="free", capability_tier=3, lifts=lifts),
        _rec("bad", outcome="not_accepted", attempts=3, capacity_class=None),
        _rec("env", outcome=None, excluded_reason="environment_error", attempts=0),
        _rec("legacy"),  # written before the labels: no outcome, not excluded
    ]  # fmt: skip

    report = evaluate(records)

    assert report.records == 4
    assert (report.labelled_records, report.excluded_records) == (2, 1)
    assert report.excluded_reasons == {"environment_error": 1}
    assert report.accepted_rate == 0.5  # over the two with an outcome, not 1 of 4
    assert report.verified_rate == pytest.approx(1 / 3)  # the legacy record still counts here
    assert report.mean_attempts == 2.0
    assert report.per_capacity_class == {"free": {"records": 1, "accepted": 1}}
    assert report.lift_counts["context_pack"] == {"present": 1, "absent": 0, "unknown": 1}
    assert report.lift_counts["lint_autofix"] == {"present": 0, "absent": 1, "unknown": 1}
    assert report.lift_counts["template"] == {"present": 0, "absent": 0, "unknown": 2}


@pytest.mark.parametrize("excluded_security_score", [0.1, 0.9])
def test_excluded_security_records_contribute_neither_false_negatives_nor_observations(
    excluded_security_score: float,
) -> None:
    role = "implementation_worker"
    included = [
        _rec("missed", outcome="not_accepted", role=role, security_relevant=True,
             security_sensitive=0.1),
        _rec("detected", outcome="accepted", verified=True, role=role, security_relevant=True,
             security_sensitive=0.9),
    ]  # fmt: skip
    excluded = _rec(
        "env",
        excluded_reason="environment_error",
        role=role,
        security_relevant=True,
        security_sensitive=excluded_security_score,
    )

    report = evaluate([*included, excluded])

    assert report.usable_signals == 3  # collection counts still include excluded runs
    assert report.excluded_records == 1
    assert report.security_fn_rate == 0.5
    assert report.per_role[role]["security_fn_rate"] == 0.5
    assert evaluate([excluded]).security_fn_rate is None  # unknown, not a task failure or zero


def test_an_excluded_record_with_a_task_outcome_is_refused() -> None:
    with pytest.raises(ValueError, match="excluded"):
        _rec("x", outcome="accepted", excluded_reason="environment_error")
    with pytest.raises(ValueError, match="outcome"):
        _rec("x", outcome="maybe")
