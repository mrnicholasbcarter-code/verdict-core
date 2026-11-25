"""The run receipt shows which attempts ran under a signed worker assignment, and the waiver (#1325).

Serve records the streaming waiver in its own receipts.  The executor reads it back per attempt
(``tests/test_serve_evidence.py``) and the runtime puts it on the attempt's ``terminal`` event;
these tests prove the projection into ``receipt.json`` and the ``verdict run-receipt`` text:

* an attempt that was served under the waiver carries it;
* an attempt that was not does not, and neither does an attempt of an executor that signs nothing;
* an attempt whose serve receipts could not be read says so, with a reason and no figures.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.contracts import WorkerTerminal, WorkGraph, WorkNode
from verdict.orchestration.receipt import (
    EventLog,
    build_run_receipt,
    verify_run_receipt,
    write_run_receipt,
)

DIGEST = "a" * 64
WAIVER = {"requirement": "streaming", "reason": "verified_worker_assignment"}


def assignment_record(
    *, requests: int | None = 3, waived: int | None = 3, reason: str | None = None
) -> dict[str, Any]:
    read = reason is None
    return {
        "assignment_digest": DIGEST,
        "serve_receipts": {
            "status": "read" if read else "unavailable",
            "reason": reason,
            "requests": requests if read else None,
            "requests_waived": waived if read else None,
            "requirement_waivers": ([WAIVER] if waived else []) if read else None,
        },
    }


def run_with(tmp_path: Path, attempts: list[dict[str, Any]]) -> Path:
    """A finished one-node run whose attempts carry the given terminal data."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        "g",
        (
            WorkNode(
                "impl",
                "write impl",
                owned_files=("impl.txt",),
                verification_command=("sh", "-c", "test -f impl.txt"),
            ),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    log = EventLog(run_dir / "events.jsonl")
    log.emit("run_started", run_id="run-1", goal="g")
    for number, extra in enumerate(attempts, 1):
        log.emit("dispatch", node_id="impl", attempt=number, route_id=f"cc/route-{number}")
        log.emit(
            "terminal",
            node_id="impl",
            attempt=number,
            ok=extra.pop("ok", True),
            route_id=f"cc/route-{number}",
            reported_model=f"cc/route-{number}",
            executor_kind="live",
            harness="opencode",
            **extra,
        )
    log.emit("run_finished", outcome="BLOCKED", reason="test")
    return run_dir


def attempts_of(run_dir: Path) -> list[dict[str, Any]]:
    receipt = build_run_receipt(run_dir)
    return list(receipt["nodes"][0]["attempts"])


def test_an_attempt_served_under_the_waiver_carries_it_in_the_receipt(tmp_path: Path) -> None:
    run_dir = run_with(tmp_path, [{"worker_assignment": assignment_record()}])
    (attempt,) = attempts_of(run_dir)
    assert attempt["worker_assignment"] == assignment_record()
    serve = attempt["worker_assignment"]["serve_receipts"]
    assert serve["requirement_waivers"] == [WAIVER]
    assert serve["requests_waived"] == 3


def test_an_attempt_that_was_not_served_under_the_waiver_does_not_carry_one(tmp_path: Path) -> None:
    run_dir = run_with(
        tmp_path,
        [
            {"worker_assignment": assignment_record(requests=2, waived=2)},
            {"worker_assignment": assignment_record(requests=2, waived=0)},
            {},  # an executor that signs nothing
        ],
    )
    first, second, third = attempts_of(run_dir)
    assert first["worker_assignment"]["serve_receipts"]["requirement_waivers"] == [WAIVER]
    assert second["worker_assignment"]["serve_receipts"]["requirement_waivers"] == []
    assert second["worker_assignment"]["serve_receipts"]["requests_waived"] == 0
    assert "worker_assignment" not in third


def test_a_run_without_any_assignment_has_a_receipt_with_no_new_key(tmp_path: Path) -> None:
    receipt = build_run_receipt(run_with(tmp_path, [{}]))
    assert "worker_assignment" not in json.dumps(receipt)


def test_an_unreadable_serve_receipt_is_null_with_a_reason_never_a_clean_attempt(
    tmp_path: Path,
) -> None:
    record = assignment_record(reason="serve's receipt database is not configured")
    (attempt,) = attempts_of(run_with(tmp_path, [{"worker_assignment": record}]))
    serve = attempt["worker_assignment"]["serve_receipts"]
    assert serve["status"] == "unavailable"
    assert serve["reason"] == "serve's receipt database is not configured"
    assert serve["requests"] is None and serve["requests_waived"] is None
    assert serve["requirement_waivers"] is None


@pytest.mark.parametrize(
    "bad",
    ["a string", ["a", "list"], 7, None, {}, {"serve_receipts": "x"}],
    ids=["str", "list", "int", "none", "empty", "serve-not-a-mapping"],
)
def test_a_malformed_record_is_not_projected_as_evidence(tmp_path: Path, bad: Any) -> None:
    (attempt,) = attempts_of(run_with(tmp_path, [{"worker_assignment": bad}]))
    assert "worker_assignment" not in attempt


def test_only_the_known_fields_are_projected_and_the_credential_never_is(tmp_path: Path) -> None:
    record = assignment_record()
    record["token"] = "vwa1.should.never"  # a stray key an executor must not send
    record["serve_receipts"]["rows"] = [{"payload": "x"}]
    (attempt,) = attempts_of(run_with(tmp_path, [{"worker_assignment": record}]))
    assert attempt["worker_assignment"] == assignment_record()
    assert "vwa1" not in json.dumps(attempt)


def test_the_receipt_still_verifies_with_the_new_field(tmp_path: Path) -> None:
    run_dir = run_with(tmp_path, [{"worker_assignment": assignment_record()}])
    write_run_receipt(run_dir)
    assert verify_run_receipt(run_dir) == []


def _show(run_dir: Path, capsys: pytest.CaptureFixture[str]) -> str:
    from verdict.orchestration.cli import _receipt

    _receipt(argparse.Namespace(run=str(run_dir), runs_dir=str(run_dir.parent), json=False))
    return capsys.readouterr().out


def test_run_receipt_text_marks_the_attempt_that_ran_with_streaming_waived(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = run_with(
        tmp_path,
        [
            {"worker_assignment": assignment_record(requests=2, waived=2)},
            {"worker_assignment": assignment_record(requests=2, waived=0)},
            {"worker_assignment": assignment_record(reason="not configured")},
            {},
        ],
    )
    write_run_receipt(run_dir)
    line = next(row for row in _show(run_dir, capsys).splitlines() if row.startswith("  impl"))
    assert line.count("streaming waived") == 1
    assert "cc/route-1[success, streaming waived]" in line
    assert "cc/route-2[success]" in line
    assert "cc/route-3[success, serve receipts unavailable]" in line
    assert "cc/route-4[success]" in line


def test_the_terminal_type_carries_the_record_and_defaults_to_none() -> None:
    assert WorkerTerminal(ok=True).worker_assignment is None
    terminal = WorkerTerminal(ok=True, worker_assignment=assignment_record())
    assert terminal.worker_assignment == assignment_record()
