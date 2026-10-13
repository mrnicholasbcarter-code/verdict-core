"""Live acceptance job: green only with eligible FREE routes, one alert per streak, doctor view."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import acceptance, acceptance_state, acceptance_toolcheck
from verdict.acceptance import Check

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
NOTE = "verdict acceptance probe 7"


def _verdict(route: str, *, eligible: bool, proven: bool = True) -> dict[str, Any]:
    return {
        "route_id": route,
        "provider": route.split("/")[0],
        "capacity_class": "free",
        "reached": "TASK_ELIGIBLE" if eligible else "ENTITLED",
        "reason": "ranked" if eligible else "no_agentic_probe",
        "supports_tools": True,
        "agentic_capability": {"qualified": proven},
    }


def _good_tool_gateway(blocked: frozenset[str] = frozenset(), bad: frozenset[str] = frozenset()):
    """A fake /v1/chat/completions that passes the 2-turn check unless told otherwise."""

    def post(path: str, body: dict[str, Any]) -> tuple[int, Any]:
        route = body["model"]
        if route in blocked:
            return 403, None
        if route in bad:
            return 200, {"choices": [{"message": {"content": "no tools"}}]}
        turn2 = any(m["role"] == "tool" for m in body["messages"])
        name, args = (
            ("write_file", {"path": "out.txt", "content": NOTE.upper()})
            if turn2
            else ("read_file", {"path": "notes.txt"})
        )
        call = {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}
        return 200, {"choices": [{"message": {"content": "", "tool_calls": [call]}}]}

    return post


# -- independent 2-turn check -------------------------------------------------


def test_two_turn_check_passes_fails_and_names_account_blocks() -> None:
    post = _good_tool_gateway(blocked=frozenset({"p/blocked"}), bad=frozenset({"p/bad"}))
    assert acceptance_toolcheck.check_route("p/good", post).outcome == "pass"
    assert acceptance_toolcheck.check_route("p/bad", post).outcome == "fail"
    assert acceptance_toolcheck.check_route("p/blocked", post).outcome == "account"


def test_measure_counts_unserved_listed_routes_against_precision() -> None:
    listed = [_verdict("p/a", eligible=True), _verdict("p/ghost", eligible=True)]
    synced = {"p": [{"id": "a"}, {"id": "b"}]}
    verdicts = [*listed, _verdict("p/b", eligible=False)]
    quality = acceptance_toolcheck.measure(
        listed, verdicts, synced=synced, cooling=set(), post=_good_tool_gateway(), seed="s"
    )
    assert quality["listed_unserved"] == ["p/ghost"]
    assert quality["precision"] == pytest.approx(0.5)
    # p/b passes the independent check but is not listed: 1 found of 2.
    assert quality["recall_estimate"] == pytest.approx(0.5)


def test_measure_removes_account_blocked_routes_from_recall() -> None:
    listed = [_verdict("p/a", eligible=True)]
    synced = {"p": [{"id": "a"}], "q": [{"id": "x"}]}
    verdicts = [*listed, _verdict("q/x", eligible=False)]
    quality = acceptance_toolcheck.measure(
        listed,
        verdicts,
        synced=synced,
        cooling=set(),
        post=_good_tool_gateway(blocked=frozenset({"q/x"})),
        seed="s",
    )
    assert quality["precision"] == 1.0
    assert quality["recall_estimate"] == 1.0


def test_cooling_providers_need_every_connection_cooling() -> None:
    later = (NOW + timedelta(hours=1)).isoformat()
    rows = [
        {"provider": "a", "rateLimitedUntil": later},
        {"provider": "b", "rateLimitedUntil": later},
        {"provider": "b", "rateLimitedUntil": None},
    ]
    assert acceptance_toolcheck.cooling_providers(rows, NOW) == {"a"}


# -- the run ------------------------------------------------------------------


def _fake_cli(eligibility: dict[str, Any], *, choose_exit: int = 2):
    def run_cli(args: list[str], timeout: float, env: Any = None) -> tuple[int, str, str]:
        name = args[0]
        if name == "doctor":
            return 0, json.dumps({"status": "ok", "issues": []}), ""
        if name == "eligibility":
            return 0, json.dumps(eligibility), ""
        if name == "choose":
            return choose_exit, "", "verdict choose: error: unrecognized arguments: --role"
        if name == "route":
            return 0, json.dumps({"model": "p/a"}), ""
        return 0, json.dumps({"enabled": True, "provider": "verdict"}), ""

    return run_cli


@pytest.fixture
def live(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setattr(
        acceptance, "serve_check", lambda gateway: Check("serve", [], True, "HTTP 200")
    )
    monkeypatch.setattr(
        acceptance,
        "gateway_get",
        lambda base, key, path, timeout=60: (
            {"p": [{"id": "a"}, {"id": "b"}]} if "synced" in path else {"connections": []}
        ),
    )
    monkeypatch.setattr(acceptance_toolcheck, "gateway_post", lambda *a, **k: _good_tool_gateway())
    return tmp_path


def test_run_is_never_green_while_no_free_route_is_eligible(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"summary": {"eligible": 0}, "verdicts": [_verdict("p/a", eligible=False)]}
    # Even with choose implemented, 0 eligible FREE routes is red.
    monkeypatch.setattr(acceptance, "run_cli", _fake_cli(payload, choose_exit=0))
    result = acceptance.run_acceptance(gateway="http://gw")
    eligibility = next(c for c in result["checks"] if c["name"] == "eligibility")
    assert result["status"] == "red"
    assert result["eligible_free"] == 0
    assert eligibility["passed"] is False
    assert eligibility["reason"] == "0 FREE tool-capable routes eligible"
    assert Path(result["path"]).parent == live / "acceptance"
    assert json.loads(Path(result["path"]).read_text())["status"] == "red"


def test_is_green_requires_an_eligible_free_route_even_when_checks_pass() -> None:
    checks = [Check("eligibility", [], True, "", detail={"eligible_free": 0})]
    assert acceptance.is_green(checks) is False
    checks[0].detail["eligible_free"] = 3
    assert acceptance.is_green(checks) is True


def test_choose_without_role_support_is_a_failure_not_a_skip(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"verdicts": [_verdict("p/a", eligible=True), _verdict("p/b", eligible=True)]}
    monkeypatch.setattr(acceptance, "run_cli", _fake_cli(payload))
    result = acceptance.run_acceptance(gateway="http://gw")
    checks = {c["name"]: c for c in result["checks"]}
    assert checks["eligibility"]["passed"] is True, checks["eligibility"]["reason"]
    assert checks["choose"]["passed"] is False
    assert checks["choose"]["reason"].startswith("not implemented")
    assert result["status"] == "red"
    assert result["failed"] == ["choose"]
    assert all(c["stdout_sha256"].startswith("sha256:") for c in result["checks"][:4])


def test_listed_route_without_agentic_proof_fails_eligibility(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"verdicts": [_verdict("p/a", eligible=True, proven=False)]}
    monkeypatch.setattr(acceptance, "run_cli", _fake_cli(payload, choose_exit=0))
    result = acceptance.run_acceptance(gateway="http://gw")
    eligibility = next(c for c in result["checks"] if c["name"] == "eligibility")
    assert eligibility["passed"] is False
    assert "without agentic proof" in eligibility["reason"]


def test_doctor_judge_ignores_only_its_own_acceptance_issue() -> None:
    own = {"issues": ["Live acceptance is red (x.json): choose"]}
    assert acceptance.judge_doctor(1, json.dumps(own), "")[0] is True
    other = {"issues": ["verdict-prove-at-rest.timer is not installed"]}
    assert acceptance.judge_doctor(1, json.dumps(other), "")[0] is False


# -- alert state ----------------------------------------------------------------


def _record(directory: Path, status: str, n: int, **kw: Any) -> dict[str, Any]:
    result = {
        "status": status,
        "path": str(directory / f"2026101{n}T000000Z.json"),
        "finished_at": NOW.isoformat(),
        "failed": [] if status == "green" else ["choose"],
        "checks": [],
    }
    return acceptance_state.update_alert_state(directory, result, **kw)


def test_one_email_per_regression_streak_and_one_on_recovery(tmp_path: Path) -> None:
    kw = {"notify": "dry-run", "email": "ops@example.invalid"}
    # Red from the start is a known state: no email.
    assert _record(tmp_path, "red", 1, **kw)["event"] is None
    assert _record(tmp_path, "red", 2, **kw)["event"] is None
    assert _record(tmp_path, "green", 3, **kw)["event"] is None
    assert _record(tmp_path, "red", 4, **kw)["event"] is None
    second = _record(tmp_path, "red", 5, **kw)
    assert second["event"] == "regression"
    assert json.loads(Path(second["dry_run_file"]).read_text())["recipient_email"] == kw["email"]
    assert _record(tmp_path, "red", 6, **kw)["event"] is None
    assert _record(tmp_path, "green", 7, **kw)["event"] == "recovery"
    state = json.loads((tmp_path / "state.json").read_text())
    assert [entry["event"] for entry in state["sent"]] == ["regression", "recovery"]


def test_send_mode_records_the_message_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[Path] = []

    def fake_send(path: Path) -> dict[str, Any]:
        sent.append(path)
        return {"message_id": "m1", "thread_id": "t1"}

    monkeypatch.setattr(acceptance_state, "send_email", fake_send)
    kw = {"notify": "send", "email": "ops@example.invalid"}
    for n, status in enumerate(["green", "red", "red", "red"], start=1):
        _record(tmp_path, status, n, **kw)
    assert len(sent) == 1
    assert json.loads((tmp_path / "state.json").read_text())["sent"][0]["message_id"] == "m1"


def test_prune_keeps_the_newest_results(tmp_path: Path) -> None:
    for n in range(5):
        (tmp_path / f"2026101{n}T000000Z.json").write_text("{}")
    acceptance_state.prune(tmp_path, 2)
    assert [p.name for p in acceptance_state.result_files(tmp_path)] == [
        "20261013T000000Z.json",
        "20261014T000000Z.json",
    ]


# -- doctor view ----------------------------------------------------------------


def _write_result(home: Path, status: str, finished: datetime) -> None:
    directory = home / "acceptance"
    directory.mkdir(parents=True, exist_ok=True)
    checks = [{"name": "choose", "passed": status == "green", "reason": "not implemented"}]
    body = {"status": status, "finished_at": finished.isoformat(), "checks": checks}
    (directory / "20261013T110000Z.json").write_text(json.dumps(body))


def test_doctor_warns_without_result_and_fails_on_red_or_stale(tmp_path: Path) -> None:
    row, issues, warnings = acceptance_state.doctor_check(home=tmp_path, now=NOW)
    assert row["state"] == "none" and not issues and warnings
    _write_result(tmp_path, "green", NOW - timedelta(minutes=30))
    assert acceptance_state.doctor_check(home=tmp_path, now=NOW)[1] == []
    _write_result(tmp_path, "red", NOW - timedelta(minutes=30))
    issues = acceptance_state.doctor_check(home=tmp_path, now=NOW)[1]
    assert issues and "choose (not implemented)" in issues[0]
    _write_result(tmp_path, "green", NOW - timedelta(hours=3))
    issues = acceptance_state.doctor_check(home=tmp_path, now=NOW)[1]
    assert len(issues) == 1 and "stale" in issues[0]


def test_doctor_section_shows_the_newest_result_and_fails_on_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.doctor_diagnostics import DoctorDiagnostics, _doctor_acceptance
    from verdict.doctor_presentation import repair_command

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    _write_result(tmp_path, "red", datetime.now(timezone.utc) - timedelta(minutes=5))
    diag = DoctorDiagnostics()
    _doctor_acceptance(diag)
    assert diag.acceptance["state"] == "red"
    assert diag.issues and diag.issues[0].startswith(acceptance_state.ISSUE_PREFIX)
    label, state, detail = diag.sections[-1]
    assert (label, state) == ("Live acceptance", "failed")
    assert detail.startswith("red: ") and "min old" in detail
    assert repair_command(diag.issues[0]) == "verdict acceptance status"
