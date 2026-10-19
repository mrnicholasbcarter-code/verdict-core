"""Tests for the BOD-353 outcome-attempt writer (verdict outcome record / import).

Covers:
- fixed failure-class taxonomy
- build_attempt_record validation + model-id canonicalization
- write_attempt_record appends JSONL under VERDICT_HOME
- import_decision_jsonl best-effort maps legacy work/model-decisions.jsonl rows
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from verdict.outcome_records import (
    ATTEMPT_FAILURE_CLASSES,
    ATTEMPT_OUTCOMES,
    ATTEMPT_RECORDS_FILE,
    attempt_records_path,
    build_attempt_record,
    import_decision_jsonl,
    write_attempt_record,
)


class TestTaxonomy:
    def test_failure_classes_fixed_set(self) -> None:
        assert ATTEMPT_FAILURE_CLASSES == (
            "success",
            "test_failed",
            "review_rejected",
            "quota_429",
            "model_cooldown",
            "stream_drop",
            "timeout",
            "tool_error",
            "spec_violation",
            "false_report",
            "provider_5xx",
            "auth_error",
            "other",
        )

    def test_outcomes_fixed_set(self) -> None:
        assert ATTEMPT_OUTCOMES == ("accepted", "failed", "partial", "abandoned")


class TestBuildAttemptRecord:
    def test_canonicalizes_model_id(self) -> None:
        record = build_attempt_record(
            task_kind="implement",
            harness="prime",
            model="omniroute/kiro/claude-haiku-4.5",
            outcome="accepted",
            failure_class="success",
            source="test",
        )
        assert record.model == "kiro/claude-haiku-4.5"

    def test_rejects_unknown_failure_class(self) -> None:
        with pytest.raises(ValueError, match="failure_class"):
            build_attempt_record(
                task_kind="implement",
                harness="prime",
                model="kiro/claude-haiku-4.5",
                outcome="failed",
                failure_class="made_up_class",
                source="test",
            )

    def test_rejects_unknown_outcome(self) -> None:
        with pytest.raises(ValueError, match="outcome"):
            build_attempt_record(
                task_kind="implement",
                harness="prime",
                model="kiro/claude-haiku-4.5",
                outcome="not_a_real_outcome",
                failure_class="other",
                source="test",
            )

    def test_rejects_blank_required_field(self) -> None:
        with pytest.raises(ValueError, match="model"):
            build_attempt_record(
                task_kind="implement",
                harness="prime",
                model="   ",
                outcome="accepted",
                failure_class="success",
                source="test",
            )

    def test_optional_fields_default_none(self) -> None:
        record = build_attempt_record(
            task_kind="implement",
            harness="prime",
            model="kiro/claude-haiku-4.5",
            outcome="accepted",
            failure_class="success",
            source="test",
        )
        assert record.effort is None
        assert record.duration_s is None
        assert record.tokens_in is None
        assert record.tokens_out is None
        assert record.cost_usd is None
        assert record.story_id is None
        assert record.ts  # auto-stamped


class TestWriteAttemptRecord:
    def test_appends_jsonl_under_store_dir(self, tmp_path: Path) -> None:
        record = build_attempt_record(
            task_kind="implement",
            harness="prime",
            model="kiro/claude-haiku-4.5",
            outcome="accepted",
            failure_class="success",
            story_id="BOD-353",
            source="test",
        )
        target = write_attempt_record(record, store_dir=tmp_path)
        assert target == attempt_records_path(tmp_path)
        assert target.name == ATTEMPT_RECORDS_FILE
        lines = target.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert row["model"] == "kiro/claude-haiku-4.5"
        assert row["story_id"] == "BOD-353"

    def test_second_write_appends_not_overwrites(self, tmp_path: Path) -> None:
        for i in range(2):
            record = build_attempt_record(
                task_kind="implement",
                harness="prime",
                model=f"kiro/model-{i}",
                outcome="accepted",
                failure_class="success",
                source="test",
            )
            write_attempt_record(record, store_dir=tmp_path)
        lines = attempt_records_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2


class TestImportDecisionJsonl:
    def test_imports_legacy_rows_best_effort(self, tmp_path: Path) -> None:
        legacy = tmp_path / "model-decisions.jsonl"
        legacy.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "task": "AD-06 implement bounded executor retry from exact spec",
                            "model": "kc/nvidia/nemotron-3-super-120b-a12b:free",
                            "why": "free-first writer",
                            "outcome": "failed: rewrote autodev_run.py, no tests written",
                            "ts": "2026-10-15T16:17:49Z",
                        }
                    ),
                    json.dumps(
                        {
                            "task": "verify Kiro plan live via OmniRoute",
                            "model": "kiro/claude-haiku-4.5",
                            "why": "one real verification call",
                            "outcome": "accepted: raw content OK",
                            "ts": "2026-10-15T15:26:58Z",
                        }
                    ),
                    json.dumps(
                        {
                            "task": "verify Cursor provider after operator add",
                            "model": "cursor/claude-haiku-5-5",
                            "why": "one real verification call",
                            "outcome": "failed: 502 empty turn, quota exhausted until ~2026-10-16T03:37:51Z",
                            "ts": "2026-10-15T15:37:27Z",
                        }
                    ),
                    json.dumps(
                        {
                            "task": "BOD-341 story owner",
                            "model": "omniroute/kiro/claude-sonnet-5",
                            "why": "kiro has capacity",
                            "outcome": "pending",
                            "ts": "2026-10-15T18:47:22Z",
                        }
                    ),
                    "",  # trailing blank line must be skipped
                ]
            ),
            encoding="utf-8",
        )
        count = import_decision_jsonl(legacy, store_dir=tmp_path, source="model-decisions-import")
        assert count == 4
        lines = attempt_records_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
        rows = [json.loads(line) for line in lines]
        assert len(rows) == 4

        accepted = next(r for r in rows if r["model"] == "kiro/claude-haiku-4.5")
        assert accepted["outcome"] == "accepted"
        assert accepted["failure_class"] == "success"

        quota_row = next(r for r in rows if r["model"] == "cursor/claude-haiku-5-5")
        assert quota_row["outcome"] == "failed"
        assert quota_row["failure_class"] in ("quota_429", "provider_5xx")

        pending_row = next(r for r in rows if r["model"] == "kiro/claude-sonnet-5")
        assert pending_row["outcome"] == "abandoned"
        assert pending_row["story_id"] == "BOD-341"

    def test_import_is_additive_across_calls(self, tmp_path: Path) -> None:
        legacy = tmp_path / "model-decisions.jsonl"
        legacy.write_text(
            json.dumps({"task": "t1", "model": "m/a", "outcome": "accepted"}) + "\n",
            encoding="utf-8",
        )
        count1 = import_decision_jsonl(legacy, store_dir=tmp_path, source="test")
        count2 = import_decision_jsonl(legacy, store_dir=tmp_path, source="test")
        assert count1 == 1
        assert count2 == 1
        lines = attempt_records_path(tmp_path).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2  # importer is append-only; re-import is caller's responsibility


# ---------------------------------------------------------------------------
# BOD-437 / S09: every attempt joins an independent checker result append-only
# ---------------------------------------------------------------------------


def _s09_run(root: Path) -> Path:
    """A real run bundle: node a fails on quota, then passes; node b fails verification."""
    from datetime import datetime, timezone

    from verdict.orchestration.contracts import WorkGraph, WorkNode
    from verdict.orchestration.receipt import EventLog

    fixed = datetime(2026, 10, 19, 6, 0, 0, tzinfo=timezone.utc)
    run_dir = root / "run-s09"
    run_dir.mkdir(parents=True)
    graph = WorkGraph(
        goal="fix the failing unit test in calc.py",
        nodes=(
            WorkNode("a", "build a", owned_files=("a.py",), verification_command=("pytest",)),
            WorkNode("b", "build b", owned_files=("b.py",), verification_command=("pytest",)),
        ),
    )
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()), encoding="utf-8")
    log = EventLog(run_dir / "events.jsonl", clock=lambda: fixed)
    log.emit("run_started", run_id="run-s09", goal=graph.goal)
    log.emit("dispatch", node_id="a", attempt=1, route_id="cc/claude-sonnet-5")
    log.emit("terminal", node_id="a", attempt=1, ok=False, duration_seconds=1.0)
    log.emit("failure", node_id="a", attempt=1, category="rate_limited")
    log.emit("dispatch", node_id="a", attempt=2, route_id="gh/gpt-5")
    log.emit(
        "terminal",
        node_id="a",
        attempt=2,
        ok=True,
        duration_seconds=4.0,
        reported_model="gh/gpt-5",
        usage={"input_tokens": 120, "output_tokens": 30},
    )
    log.emit("verify", node_id="a", ok=True, command="pytest", exit_code=0)
    log.emit("node_state", node_id="a", attempt=2, state="VALIDATED")
    log.emit("dispatch", node_id="b", attempt=1, route_id="gh/gpt-5")
    log.emit("terminal", node_id="b", attempt=1, ok=True, reported_model="gh/gpt-5")
    log.emit("verify", node_id="b", ok=False, command="pytest", exit_code=1)
    log.emit("failure", node_id="b", attempt=1, category="verification_failed")
    log.emit("node_state", node_id="b", attempt=1, state="REJECTED")
    log.emit("run_finished", outcome="BLOCKED", reason="b rejected")
    return run_dir


def _ledger_rows(store: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in attempt_records_path(store).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_every_attempt_links_verified_checker_without_rewriting_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from verdict.actions.registry import run_action
    from verdict.orchestration.receipt import verify_run_receipt, write_run_receipt

    store = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(store))
    run_dir = _s09_run(tmp_path / "runs")

    # Production producer: the receipt writer records each node attempt and the
    # runtime verifier's independent result in the existing attempt ledger.
    write_run_receipt(run_dir)
    receipt_bytes = (run_dir / "receipt.json").read_bytes()
    events_bytes = (run_dir / "events.jsonl").read_bytes()
    receipt = json.loads(receipt_bytes)
    first_ledger = attempt_records_path(store).read_text(encoding="utf-8")

    rows = _ledger_rows(store)
    terminals = {
        (r["run_id"], r["node_id"], r["attempt_id"]): r
        for r in rows
        if r["record_kind"] == "terminal"
    }
    assert set(terminals) == {("run-s09", "a", "1"), ("run-s09", "a", "2"), ("run-s09", "b", "1")}
    # Terminal rows carry lineage and execution facts only, never quality credit.
    assert all(
        r["quality_credit"] is None and r["checker_digest"] is None for r in terminals.values()
    )
    quota = terminals[("run-s09", "a", "1")]
    assert (quota["outcome"], quota["failure_class"]) == ("failed", "quota_429")
    assert quota["tokens_in"] is None and quota["effective_parameters"] is None  # never fabricated
    assert terminals[("run-s09", "a", "2")]["tokens_in"] == 120

    checkers = {
        (r["run_id"], r["node_id"], r["attempt_id"]): r
        for r in rows
        if r["record_kind"] == "checker_result"
    }
    # The quota failure is operational: no checker row, never in the denominator.
    assert set(checkers) == {("run-s09", "a", "2"), ("run-s09", "b", "1")}
    assert checkers[("run-s09", "a", "2")]["quality_credit"] == 1.0
    assert checkers[("run-s09", "b", "1")]["quality_credit"] == 0.0
    assert checkers[("run-s09", "b", "1")]["failure_class"] == "test_failed"
    assert {r["checker_digest"] for r in checkers.values()} == {receipt["events_digest"]}

    # Re-recording is idempotent and append-only; the run bundle never changes.
    write_run_receipt(run_dir)
    assert attempt_records_path(store).read_text(encoding="utf-8") == first_ledger
    assert (run_dir / "receipt.json").read_bytes() == receipt_bytes
    assert (run_dir / "events.jsonl").read_bytes() == events_bytes
    assert verify_run_receipt(run_dir) == []

    # A self-claimed pass from the controller CLI cannot mint quality credit.
    claim = {
        "task_kind": "implement",
        "harness": "prime",
        "model": "gh/gpt-5",
        "outcome": "accepted",
        "failure_class": "success",
        "source": "cli",
        "store_dir": str(store),
        "run_id": "run-s09",
        "node_id": "b",
        "attempt_id": "1",
        "record_kind": "checker_result",
        "quality_credit": 1.0,
    }
    refused = run_action("outcome.record", claim)
    assert not refused.ok and refused.exit_code == 2
    # Even with a forged digest the row is written but never joins as learning input.
    forged = run_action("outcome.record", {**claim, "checker_digest": "sha256:" + "0" * 64})
    assert forged.ok

    from verdict.outcome_shadow import load_checked_attempts

    joined, rejected = load_checked_attempts(
        run_dir, store_dir=store, as_of=datetime(2026, 10, 20, tzinfo=timezone.utc)
    )
    credit = {(j.node_id, j.attempt_id): j.quality_credit for j in joined}
    assert credit == {("a", "2"): 1.0, ("b", "1"): 0.0}
    assert any(r["reason"] == "checker_digest_mismatch" for r in rejected)
    assert (run_dir / "receipt.json").read_bytes() == receipt_bytes


def test_short_write_never_leaves_a_torn_ledger_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.outcome_records as outcome_records

    real_write = os.write
    calls = {"n": 0}

    def short_first_write(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[:7])
        return real_write(fd, data)

    monkeypatch.setattr(outcome_records.os, "write", short_first_write)
    for model in ("gh/gpt-5", "cc/claude-sonnet-5"):
        record = build_attempt_record(
            task_kind="implement",
            harness="prime",
            model=model,
            outcome="failed",
            failure_class="timeout",
            source="test",
        )
        write_attempt_record(record, store_dir=tmp_path)

    raw = attempt_records_path(tmp_path).read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert [json.loads(line)["model"] for line in raw.splitlines()] == [
        "gh/gpt-5",
        "cc/claude-sonnet-5",
    ]


def test_rewritten_checker_row_is_tampered_not_joined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime, timezone

    from verdict.orchestration.receipt import write_run_receipt
    from verdict.outcome_shadow import load_checked_attempts

    store = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(store))
    run_dir = _s09_run(tmp_path / "runs")
    write_run_receipt(run_dir)
    ledger = attempt_records_path(store)
    rows = _ledger_rows(store)
    for row in rows:
        if row["record_kind"] == "checker_result" and row["node_id"] == "a":
            # Same credit, but an earlier self-reported time and a different verdict.
            row["ts"] = "2026-10-18T00:00:00+00:00"
            row["outcome"] = "failed"
    ledger.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    # as_of is before the verified check time: only the forged ts could pass it.
    joined, rejected = load_checked_attempts(
        run_dir, store_dir=store, as_of=datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
    )
    assert joined == ()
    assert {"source": "run-s09:a:2", "reason": "tampered"} in rejected

    joined, _ = load_checked_attempts(
        run_dir, store_dir=store, as_of=datetime(2026, 10, 20, tzinfo=timezone.utc)
    )
    assert {(j.node_id, j.attempt_id) for j in joined} == {("b", "1")}
    assert all(j.checked_at == datetime(2026, 10, 19, 6, tzinfo=timezone.utc) for j in joined)


def test_zero_byte_write_raises_instead_of_spinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import verdict.outcome_records as outcome_records

    monkeypatch.setattr(outcome_records.os, "write", lambda fd, data: 0)
    record = build_attempt_record(
        task_kind="implement",
        harness="prime",
        model="gh/gpt-5",
        outcome="failed",
        failure_class="timeout",
        source="test",
    )
    with pytest.raises(OSError, match="no progress"):
        write_attempt_record(record, store_dir=tmp_path)


# --- slice 2: the HTTP relay records one terminal attempt row per sent attempt ---

_RESP_DELTA = b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'


def _resp_terminal(kind: str) -> bytes:
    return b"data: " + json.dumps({"type": kind, "response": {}}).encode() + b"\n\n"


@pytest.mark.parametrize(
    ("surface", "chunks", "error", "outcome", "failure_class"),
    [
        ("chat", "standard", None, "accepted", "success"),
        ("responses", "response.completed", None, "accepted", "success"),
        ("responses", "response.failed", None, "failed", "other"),
        ("responses", "response.incomplete", None, "partial", "stream_drop"),
        ("chat", "first_only", "timeout", "failed", "timeout"),
        ("chat", "no_terminal", None, "failed", "stream_drop"),
    ],
)
def test_relay_stream_attempt_reaches_ledger_with_its_terminal_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    chunks: str,
    error: str | None,
    outcome: str,
    failure_class: str,
) -> None:
    import httpx
    from fastapi.testclient import TestClient

    import verdict.api as api
    from tests.test_proxy import _configure_test_app
    from tests.test_stream_relay_wiring import CHAT_STANDARD, StreamTransport, _post

    wire = {
        "standard": CHAT_STANDARD,
        "first_only": CHAT_STANDARD[:1],
        "no_terminal": CHAT_STANDARD[:1],
    }.get(chunks) or [_RESP_DELTA, _resp_terminal(chunks)]
    transport = StreamTransport(wire, error=httpx.ReadTimeout("slow") if error else None)
    _configure_test_app(monkeypatch, transport)
    store = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(store))

    with TestClient(api.app, raise_server_exceptions=False) as client:
        _post(client, surface, f"ledger-{surface}-{chunks}")

    (row,) = _ledger_rows(store)
    assert (row["outcome"], row["failure_class"]) == (outcome, failure_class)
    assert row["record_kind"] == "terminal"
    # Transport or stream success is execution evidence, never checked quality.
    assert row["quality_credit"] is None and row["checker_digest"] is None
    assert row["request_id"] == "request-1"
    assert row["model"] == "selected-model"
    assert row["task_kind"] == surface
    assert len(str(row["attempt_id"])) == 32  # the committed dispatch permit's attempt id
    assert str(row["profile_digest"]).startswith("sha256:")
    # The fixture stream reports only total_tokens: in/out are unknown, not zero.
    assert row["tokens_in"] is None and row["tokens_out"] is None


def test_relay_buffered_attempt_records_observed_usage_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import verdict.api as api
    from tests.test_proxy import RecordingTransport, _configure_test_app

    _configure_test_app(monkeypatch, RecordingTransport())
    store = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(store))
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": "preserve all fields"}],
                "temperature": 0.2,
            },
        )
    assert response.status_code == 200

    (row,) = _ledger_rows(store)
    assert (row["outcome"], row["failure_class"], row["record_kind"]) == (
        "accepted",
        "success",
        "terminal",
    )
    assert row["quality_credit"] is None
    assert row["tokens_in"] is None and row["cost_usd"] is None
    assert row["effective_parameters"] == {"temperature": 0.2}


def test_relay_rejected_before_send_keeps_lineage_without_a_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    import verdict.api as api
    from tests.test_proxy import RecordingTransport, _configure_test_app
    from verdict.orchestration.health_cache import HealthCache

    transport = RecordingTransport()
    _configure_test_app(monkeypatch, transport)
    monkeypatch.setattr(HealthCache, "commit_dispatch", lambda self, *a, **k: None)
    store = tmp_path / "verdict-home"
    monkeypatch.setenv("VERDICT_HOME", str(store))
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "preserve all fields"}]},
        )
    assert response.status_code == 503
    assert not [r for r in transport.requests if "chat/completions" in r["url"]]

    (row,) = _ledger_rows(store)
    assert (row["outcome"], row["failure_class"], row["record_kind"]) == (
        "abandoned",
        "other",
        "terminal",
    )
    assert row["request_id"] == "request-1" and row["model"] == "selected-model"
    assert len(str(row["attempt_id"])) == 32
    assert row["quality_credit"] is None
    assert row["tokens_in"] is None and row["cost_usd"] is None
