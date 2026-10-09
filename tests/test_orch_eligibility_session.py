"""Phase 1 session evidence narrows the existing FREE agentic gate."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.test_orch_eligibility import NOW, REQ, conn, make_ladder, row
from verdict.orchestration.contracts import EligibilityStage, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.orchestration.session_evidence import SessionLedger, SessionOutcome

ROUTE = "gl/glm-5"


def setup_ladder(
    tmp_path: Path, *, attach: bool = True
) -> tuple[EligibilityLadder, SessionLedger, HealthCache]:
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record(
        ROUTE,
        ProbeResult(
            category=CATEGORY_OK, chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        ),
        NOW,
    )
    ledger = SessionLedger(tmp_path / "sessions.jsonl")
    kwargs = {"session_ledger": ledger} if attach else {}
    ladder, _ = make_ladder(
        tmp_path,
        [row(ROUTE, owned_by="glm", pricing={"input": 0.0, "output": 0.0})],
        [conn("glm", auth="apikey", plan="free", free_only=True)],
        health_cache=cache,
        **kwargs,
    )
    return ladder, ledger, cache


def outcome(pass_: bool, *, days: int = 0, failure: str = "wrong_result") -> SessionOutcome:
    return SessionOutcome(
        ROUTE,
        "pass" if pass_ else "fail",
        "real_task",
        None if pass_ else failure,
        True,
        NOW - timedelta(days=days),
        "controller",
    )


def test_agentic_pass_with_session_failures_is_rejected(tmp_path: Path) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)
    ledger.append(outcome(False))
    ledger.append(outcome(False, days=1))
    selected, verdicts = ladder.select(REQ, now=NOW)
    assert selected is None
    assert verdicts[0].failed_stage is EligibilityStage.TASK_ELIGIBLE
    assert verdicts[0].reason == "session_evidence_insufficient"


def test_passes_admit_and_expose_rank_components(tmp_path: Path) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)
    ledger.append(outcome(True))
    selected, _ = ladder.select(REQ, now=NOW)
    assert selected is not None
    assert selected.rank_components is not None
    assert selected.rank_components["session_score"] == pytest.approx(2 / 3)
    assert selected.rank_components["session_passes"] == 1
    assert selected.rank_components["session_fails"] == 0


@pytest.mark.parametrize("attach", [False, True])
def test_no_ledger_or_no_history_is_unchanged(tmp_path: Path, attach: bool) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path, attach=attach)
    if not attach:
        ledger.append(outcome(False))
    selected, _ = ladder.select(REQ, now=NOW)
    assert selected is not None
    assert selected.rank_components is not None
    assert selected.rank_components["session_score"] == (0.5 if attach else None)


def test_recent_false_claim_overrides_even_a_passing_score(tmp_path: Path) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)
    for day in range(3):
        ledger.append(outcome(True, days=day))
    ledger.append(outcome(False, failure="false_success_claim", days=7))
    assert ledger.summarize(ROUTE, NOW).score > 0.5
    assert ladder.evaluate(REQ, now=NOW)[0].reason == "recent_false_claim"


def test_old_false_claim_weights_score_but_does_not_veto(tmp_path: Path) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)
    for day in range(2):
        ledger.append(outcome(True, days=day))
    ledger.append(outcome(False, failure="false_success_claim", days=8))
    assert ledger.summarize(ROUTE, NOW).score == 0.5
    assert ladder.select(REQ, now=NOW)[0] is not None


def test_ledger_does_not_replace_agentic_or_apply_to_frontier(tmp_path: Path) -> None:
    ladder, ledger, cache = setup_ladder(tmp_path)
    ledger.append(outcome(True))
    cache._routes[ROUTE] = replace(cache._routes[ROUTE], agentic_ok=False)
    assert ladder.evaluate(REQ, now=NOW)[0].reason == "no_agentic_probe"
    ledger.append(outcome(False, failure="false_success_claim", days=1))
    assert ladder.select(TaskRequirements(frontier_worthy=True), now=NOW)[0] is not None


@pytest.mark.parametrize("days,verified", [(15, True), (0, False)])
def test_old_or_unverified_failures_do_not_block(tmp_path: Path, days: int, verified: bool) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)
    ledger.append(
        replace(
            outcome(False, failure="false_success_claim", days=days),
            verified_by_controller=verified,
        )
    )
    assert ladder.select(REQ, now=NOW)[0] is not None


def test_score_is_exposed_but_does_not_change_sort_order(tmp_path: Path) -> None:
    ladder, ledger, cache = setup_ladder(tmp_path)
    other = "gl/glm-6"
    ladder._rows[other] = row(other, owned_by="glm", pricing={"input": 0.0, "output": 0.0})
    cache._routes[other] = replace(cache._routes[ROUTE], route_id=other)
    ledger.append(outcome(True))
    for day in range(3):
        ledger.append(replace(outcome(True, days=day), route_id=other))
    chosen, verdicts = ladder.select(REQ, now=NOW)
    assert chosen is not None and chosen.route_id == ROUTE  # existing lexicographic tie break
    by_id = {v.route_id: v for v in verdicts}
    assert by_id[other].rank_components["session_score"] > chosen.rank_components["session_score"]


def test_cli_json_exposes_attached_session_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json
    from datetime import datetime, timezone

    from tests.test_orchestration_eligibility_output import _args
    from verdict.orchestration import cli as orch_cli
    from verdict.orchestration import eligibility_report

    ladder, ledger, _ = setup_ladder(tmp_path)
    ledger.append(replace(outcome(True), at=datetime.now(timezone.utc)))
    monkeypatch.setattr(eligibility_report, "build_selector", lambda *a, **kw: ladder)
    # This mocked CLI is read-only and makes no inventory, gateway or provider calls.
    assert orch_cli.dispatch(_args(json=True, probe=False, frontier=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    components = payload["verdicts"][0]["rank_components"]
    assert components["session_score"] == pytest.approx(2 / 3)
    assert components["session_passes"] == 1
    assert components["session_fails"] == 0


@pytest.mark.parametrize("frontier", [False, True])
@pytest.mark.parametrize("non_free", [False, True])
@pytest.mark.parametrize("method", ["select", "evaluate"])
def test_unreadable_ledger_fails_open_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, frontier: bool, non_free: bool, method: str
) -> None:
    ladder, ledger, cache = setup_ladder(tmp_path)
    other = "gl/glm-6"
    ladder._rows[other] = row(other, owned_by="glm", pricing={"input": 0.0, "output": 0.0})
    cache._routes[other] = replace(cache._routes[ROUTE], route_id=other)
    if non_free:
        ladder._connections = [conn("glm")]
    ledger.append(outcome(False))
    attempts = 0
    real_load = ledger.load

    def track_load() -> tuple[SessionOutcome, ...]:
        nonlocal attempts
        attempts += 1
        return real_load()

    monkeypatch.setattr(ledger, "load", track_load)
    ledger.path.chmod(0o000)
    try:
        requirements = TaskRequirements(frontier_worthy=frontier)
        if method == "select":
            selected, verdicts = ladder.select(requirements, now=NOW)
            assert selected is not None
        else:
            verdicts = ladder.evaluate(requirements, now=NOW)
        assert all(v.failed_stage is None for v in verdicts)
        assert all(v.rank_components["session_score"] is None for v in verdicts)
        assert all(v.rank_components["session_evidence_error"] == "unreadable" for v in verdicts)
        assert attempts == 1
    finally:
        ledger.path.chmod(0o600)
    # A new selection retries the repaired file, rather than hiding evidence forever.
    if not frontier and not non_free:
        assert ladder.evaluate(REQ, now=NOW)[0].reason == "session_evidence_insufficient"


@pytest.mark.parametrize("frontier", [False, True])
def test_corrupt_ledger_preserves_valid_evidence_and_returns_verdicts(
    tmp_path: Path, frontier: bool
) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)
    ledger.append(outcome(False))
    ledger.path.write_text("not valid json\n" + ledger.path.read_text())
    selected, verdicts = ladder.select(TaskRequirements(frontier_worthy=frontier), now=NOW)
    assert ledger.last_load_skipped == 1
    if frontier:
        assert selected is not None
    else:
        assert selected is None
        assert verdicts[0].reason == "session_evidence_insufficient"


def test_other_ledger_os_errors_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ladder, ledger, _ = setup_ladder(tmp_path)

    def broken_load() -> tuple[SessionOutcome, ...]:
        raise OSError("I/O error")

    monkeypatch.setattr(ledger, "load", broken_load)
    selected, _ = ladder.select(REQ, now=NOW)
    assert selected is not None
    assert selected.rank_components["session_evidence_error"] == "unreadable"
