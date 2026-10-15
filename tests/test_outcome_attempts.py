"""Tests for the BOD-353 outcome-attempt writer (verdict outcome record / import).

Covers:
- fixed failure-class taxonomy
- build_attempt_record validation + model-id canonicalization
- write_attempt_record appends JSONL under VERDICT_HOME
- import_decision_jsonl best-effort maps legacy work/model-decisions.jsonl rows
"""

from __future__ import annotations

import json
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
