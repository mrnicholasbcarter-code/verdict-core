"""Controller-verified session evidence is separate from probe health."""

from __future__ import annotations

import fcntl
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.session_evidence import (
    SessionLedger,
    SessionOutcome,
    import_worker_outcomes,
)

NOW = datetime(2026, 10, 9, 19, tzinfo=timezone.utc)
ROUTE = "kilocode/nvidia/nemotron-3-super-120b-a12b:free"
FIXTURE = Path(__file__).parent / "fixtures" / "session_worker_outcomes.jsonl"


def evidence(outcome: str = "pass", **changes: object) -> SessionOutcome:
    item = SessionOutcome(ROUTE, "pass", "session_canary_v1", None, True, NOW, "controller")
    if outcome == "fail":
        item = replace(item, outcome="fail", failure_class="wrong_result")
    return replace(item, **changes)


def test_round_trip_and_locked_concurrent_appends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = SessionLedger(tmp_path / "nested" / "evidence.jsonl")
    assert ledger.load() == ()
    items = (evidence(), evidence("fail", at=NOW - timedelta(seconds=1)))
    locks: list[int] = []
    real_flock = fcntl.flock

    def track_lock(fd: int, operation: int) -> None:
        locks.append(operation)
        real_flock(fd, operation)

    monkeypatch.setattr(fcntl, "flock", track_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(ledger.append, items))
    assert set(SessionLedger(ledger.path).load()) == set(items)
    assert locks.count(fcntl.LOCK_EX) == 2
    assert len(ledger.path.read_text().splitlines()) == 2
    ledger.append(items[0])
    assert len(ledger.load()) == 2  # repeated imports do not inflate success
    assert json.loads(ledger.path.read_text().splitlines()[0])["verified_by_controller"] is True


def test_score_weights_false_claims_and_bounds_window(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "evidence.jsonl")
    for item in (
        evidence(),
        evidence("fail", at=NOW - timedelta(days=1)),
        evidence("fail", failure_class="false_success_claim", at=NOW - timedelta(days=2)),
        evidence("fail", at=NOW - timedelta(days=15)),
        evidence("fail", at=NOW + timedelta(seconds=1)),
        evidence("fail", verified_by_controller=False, at=NOW - timedelta(days=3)),
        evidence("fail", route_id="other"),
    ):
        ledger.append(item)
    stats = ledger.summarize(ROUTE, NOW)
    assert (stats.passes, stats.fails, stats.false_claims) == (1, 3, 1)
    assert stats.score == pytest.approx(2 / 6)
    assert stats.last_pass_at == NOW
    assert stats.last_fail_at == NOW - timedelta(days=1)
    empty = ledger.summarize("missing", NOW)
    assert (empty.passes, empty.fails, empty.false_claims, empty.score) == (0, 0, 0, 0.5)
    assert empty.last_pass_at is empty.last_fail_at is None
    assert ledger.summarize(ROUTE, NOW, window_days=1).score == 0.5


def test_default_path_and_env_override_are_tmp_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VERDICT_SESSION_EVIDENCE", raising=False)
    assert SessionLedger().path == tmp_path / ".verdict" / "session-evidence.jsonl"
    override = tmp_path / "custom.jsonl"
    monkeypatch.setenv("VERDICT_SESSION_EVIDENCE", str(override))
    ledger = SessionLedger()
    ledger.append(evidence())
    assert ledger.path == override
    assert not (tmp_path / ".verdict").exists()


def test_import_real_controller_fixture_is_idempotent(tmp_path: Path) -> None:
    assert len(FIXTURE.read_text().splitlines()) == 8
    imported = import_worker_outcomes(FIXTURE)
    assert len(imported) == 7
    assert sum(item.outcome == "pass" for item in imported) == 4
    assert {item.failure_class for item in imported if item.outcome == "fail"} == {
        "gave_up",
        "narrates_tool_calls",
        "false_success_claim",
    }
    assert all(item.verified_by_controller and item.source == str(FIXTURE) for item in imported)
    duplicate = tmp_path / "duplicates.jsonl"
    duplicate.write_text(FIXTURE.read_text() * 2)
    assert len(import_worker_outcomes(duplicate)) == 7
    ledger = SessionLedger(tmp_path / "imported.jsonl")
    for item in imported + import_worker_outcomes(FIXTURE):
        ledger.append(item)
    assert ledger.load() == tuple(imported)
    stats = ledger.summarize(ROUTE, NOW)
    assert (stats.passes, stats.fails, stats.false_claims) == (1, 2, 1)
    assert stats.score == 0.4


@pytest.mark.parametrize(
    "error,failure",
    [
        ("narrates_tool_calls", "narrates_tool_calls"),
        ("harness_path_block+gave_up", "gave_up"),
        ("upstream HTTP 504", "transport_error"),
        ("timeout", "timeout"),
        ("timed out", "timeout"),
        ("incorrect file contents", "wrong_result"),
    ],
)
def test_importer_maps_failure_classes(tmp_path: Path, error: str, failure: str) -> None:
    source = tmp_path / "worker.jsonl"
    source.write_text(
        json.dumps(
            {
                "route": ROUTE,
                "kind": "session_canary_v1",
                "ts": NOW.isoformat(),
                "outcome": "SESSION_CANARY_FAIL",
                "error": error,
            }
        )
        + "\n"
    )
    assert import_worker_outcomes(source)[0].failure_class == failure


def test_importer_normalizes_timestamp_identity_and_ignores_unknown_prefixes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "worker.jsonl"
    base = {
        "route": ROUTE,
        "kind": "session_canary_v1",
        "ts": NOW.isoformat(),
        "outcome": "SESSION_CANARY_PASS",
        "error": None,
    }
    source.write_text(
        "\n"
        + "\n".join(
            json.dumps(item)
            for item in [
                base,
                {**base, "ts": NOW.isoformat().replace("+00:00", "Z")},
                {**base, "kind": "other"},
                {**base, "outcome": "unknown"},
                {**base, "kind": "free_real_task", "outcome": "unknown"},
            ]
        )
        + "\n"
    )
    assert len(import_worker_outcomes(source)) == 1


def test_invalid_outcomes_and_naive_timestamps_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="failure_class"):
        evidence("fail", failure_class="invented")
    with pytest.raises(ValueError, match="pass cannot"):
        evidence(failure_class="timeout")
    with pytest.raises(ValueError, match="verified_by_controller"):
        evidence(verified_by_controller="true")
    with pytest.raises(ValueError):
        evidence(at=NOW.replace(tzinfo=None))
    ledger = SessionLedger(tmp_path / "evidence.jsonl")
    with pytest.raises(ValueError, match="window_days"):
        ledger.summarize(ROUTE, NOW, window_days=-1)
    assert not ledger.path.exists()


@pytest.mark.parametrize(
    "bad_line", ["not valid json", "null", "[]", "{}", '{"route_id":null}', '{"at":"invalid"}']
)
def test_load_skips_and_counts_bad_lines_without_losing_valid_evidence(
    tmp_path: Path, bad_line: str
) -> None:
    ledger = SessionLedger(tmp_path / "evidence.jsonl")
    ledger.append(evidence())
    valid_line = ledger.path.read_text()
    ledger.path.write_text(bad_line + "\n\n" + valid_line)
    assert ledger.load() == (evidence(),)
    assert ledger.last_load_skipped == 1
    assert ledger.summarize(ROUTE, NOW).passes == 1
    ledger.append(evidence("fail", at=NOW - timedelta(seconds=1)))
    assert len(ledger.load()) == 2
    assert ledger.last_load_skipped == 1
    ledger.path.write_text(valid_line)
    assert ledger.load() == (evidence(),)
    assert ledger.last_load_skipped == 0
    ledger.path.unlink()
    assert ledger.load() == ()
    assert ledger.last_load_skipped == 0


def test_load_skips_non_utf8_content(tmp_path: Path) -> None:
    ledger = SessionLedger(tmp_path / "evidence.jsonl")
    ledger.append(evidence())
    ledger.path.write_bytes(b"\xff\n" + ledger.path.read_bytes())
    assert ledger.load() == (evidence(),)
    assert ledger.last_load_skipped == 1


@pytest.mark.parametrize(
    "first,alias",
    [
        ("agy/claude-opus-4-6-thinking", "antigravity/claude-opus-4-6-thinking"),
        ("kc/vendor/model:free", "openrouter/vendor/model:free"),
    ],
)
def test_pool_summary_combines_aliases_but_not_other_models_or_pools(
    tmp_path: Path, first: str, alias: str
) -> None:
    ledger = SessionLedger(tmp_path / "evidence.jsonl")
    ledger.append(evidence(route_id=first))
    ledger.append(evidence("fail", route_id=alias))
    ledger.append(evidence("fail", route_id=first + "-other"))
    ledger.append(evidence("fail", route_id="independent/" + first.split("/", 1)[1]))
    ledger.append(evidence("fail", route_id=alias, at=NOW - timedelta(days=15)))
    ledger.append(evidence("fail", route_id=alias, at=NOW + timedelta(seconds=1)))
    ledger.append(evidence("fail", route_id=alias, verified_by_controller=False))
    stats = ledger.summarize_pool(alias, NOW)
    assert (stats.passes, stats.fails, stats.false_claims, stats.score) == (1, 1, 0, 0.5)
    assert ledger.summarize_pool(first, NOW) == stats
    assert ledger.summarize(first, NOW).fails == 0  # exact-route API stays exact
    with pytest.raises(ValueError, match="window_days"):
        ledger.summarize_pool(alias, NOW, window_days=-1)
