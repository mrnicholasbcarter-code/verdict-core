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


def test_measure_treats_v1_models_routes_as_served_even_when_synced_omits_the_provider() -> None:
    # ollama-cloud is entirely absent from the synced payload (A39/BOD-439): the
    # gateway's /v1/models catalog is the second evidence source that must be
    # unioned in before a listed route is called "unserved".
    listed = [_verdict("p/a", eligible=True), _verdict("ollama-cloud/gemma4:31b", eligible=True)]
    synced = {"p": [{"id": "a"}]}
    catalog = {"data": [{"id": "ollama-cloud/gemma4:31b"}]}
    quality = acceptance_toolcheck.measure(
        listed,
        listed,
        synced=synced,
        catalog=catalog,
        cooling=set(),
        post=_good_tool_gateway(),
        seed="s",
    )
    assert quality["listed_unserved"] == []
    assert {c["route"] for c in quality["checks"]} == {"p/a", "ollama-cloud/gemma4:31b"}


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
        # Every check is pointed at the gateway the job measures.
        assert env is not None and env["OMNIROUTE_BASE_URL"] == "http://gw"
        name = args[0]
        if name == "doctor":
            return 0, json.dumps({"status": "ok", "issues": []}), ""
        if name == "eligibility":
            return 0, json.dumps(eligibility), ""
        if name == "choose":
            # main's choose CLI: --task-class is required; there is no --role.
            assert "--task-class" in args and "--role" not in args, args
            return (
                choose_exit,
                "",
                (
                    "verdict choose requires --candidates-json until live catalog probing "
                    "is in a later story"
                ),
            )
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
    assert eligibility["reason"].startswith("0 FREE tool-capable routes eligible")
    # The independent check still runs and shows what the empty list misses.
    assert eligibility["detail"]["independent_check"]["recall_estimate"] == 0.0
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


def test_judge_doctor_fails_closed_on_error_or_malformed_report() -> None:
    # Crashed doctor with error payload: regression from release review
    passed, reason, detail = acceptance.judge_doctor(1, '{"error":"doctor crashed"}', "")
    assert passed is False
    assert "doctor error (exit 1)" in reason
    assert detail.get("error") == "doctor crashed"

    # Missing issues list (malformed schema)
    passed, reason, _ = acceptance.judge_doctor(1, "{}", "")
    assert passed is False
    assert "malformed" in reason

    passed, reason, _ = acceptance.judge_doctor(0, '{"status": "ok"}', "")
    assert passed is False
    assert "malformed" in reason

    passed, reason, _ = acceptance.judge_doctor(0, '{"issues": "not-a-list"}', "")
    assert passed is False
    assert "malformed" in reason

    # Nonzero exit with empty issues list cannot pass implicitly
    passed, reason, _ = acceptance.judge_doctor(1, '{"issues": []}', "unexpected crash")
    assert passed is False
    assert "without reporting issues" in reason

    # Non-JSON / crash
    passed, reason, _ = acceptance.judge_doctor(1, "Internal Server Error", "")
    assert passed is False
    assert "printed no JSON" in reason

    # R3 regression: timeout 124 or signal 137 with own-only warning must still FAIL
    own_report = json.dumps(
        {"status": "failed", "issues": [acceptance.ISSUE_PREFIX + " (x.json): red"]}
    )
    passed, reason, detail = acceptance.judge_doctor(124, own_report, "timed out after 600s")
    assert passed is False
    assert "exit 124" in reason

    passed, reason, detail = acceptance.judge_doctor(137, own_report, "killed")
    assert passed is False
    assert "exit 137" in reason

    # Error payload detail must be redacted
    secret_err = "fatal: token sk-ant-live1234567890abcdef failed"
    passed, reason, detail = acceptance.judge_doctor(1, json.dumps({"error": secret_err}), "")
    assert passed is False
    assert "sk-ant-live1234567890abcdef" not in detail.get("error", "")


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


def test_serve_check_gives_authenticated_serve_a_receipts_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    seen: dict[str, Any] = {}

    class _Exited:
        returncode = 3

        def __init__(self, argv: list[str], **kw: Any) -> None:
            seen.update(kw["env"])
            kw["stderr"].write("boom")

        def poll(self) -> int:
            return 3

        def terminate(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 3

    monkeypatch.setattr(acceptance.subprocess, "Popen", _Exited)
    check = acceptance.serve_check("http://gw", startup_seconds=1)
    assert check.passed is False and check.reason == "serve exited 3"
    assert check.detail["stderr_tail"] == "boom"
    assert seen["VERDICT_RECEIPTS_DB"] == str(tmp_path / "acceptance" / "serve-receipts.db")
    assert seen["LLMGATE_AUTH_TOKEN"]


def test_a_second_concurrent_run_exits_2_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl

    from verdict.cli import cmd_acceptance_run

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    directory = tmp_path / "acceptance"
    directory.mkdir()
    with open(directory / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        with pytest.raises(SystemExit) as exc:
            cmd_acceptance_run()
    assert exc.value.code == 2
    assert "another acceptance run is active" in capsys.readouterr().err
    assert acceptance_state.result_files(directory) == []


def test_a_crashing_check_is_a_recorded_failure_not_a_lost_result(
    live: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = {"verdicts": [_verdict("p/a", eligible=True)]}
    monkeypatch.setattr(acceptance, "run_cli", _fake_cli(payload, choose_exit=0))

    def boom(gateway: str) -> Check:
        raise ConnectionResetError("serve went away")

    monkeypatch.setattr(acceptance, "serve_check", boom)
    result = acceptance.run_acceptance(gateway="http://gw")
    serve = next(c for c in result["checks"] if c["name"] == "serve")
    assert serve["passed"] is False and "ConnectionResetError" in serve["reason"]
    assert result["status"] == "red"
    assert Path(result["path"]).is_file()


def test_recall_estimate_drops_account_blocked_share_of_the_population() -> None:
    # 1 pass, 1 fail, 2 account-blocked among 4 unlisted samples; population 4.
    listed = [_verdict("p/a", eligible=True)]
    unlisted = ["q/1", "q/2", "q/3", "q/4"]
    synced = {"p": [{"id": "a"}], "q": [{"id": u.split("/")[1]} for u in unlisted]}
    verdicts = [*listed, *(_verdict(u, eligible=False) for u in unlisted)]
    post = _good_tool_gateway(blocked=frozenset({"q/3", "q/4"}), bad=frozenset({"q/2"}))
    quality = acceptance_toolcheck.measure(
        listed, verdicts, synced=synced, cooling=set(), post=post, seed="s"
    )
    # Unblocked population 2, pass rate 1/2 -> 1 route missed; 1 found -> recall 0.5.
    assert quality["estimated_missed"] == pytest.approx(1.0)
    assert quality["recall_estimate"] == pytest.approx(0.5)


def test_recall_is_unmeasured_when_every_unlisted_sample_is_account_blocked() -> None:
    listed = [_verdict("p/a", eligible=True)]
    synced = {"p": [{"id": "a"}], "q": [{"id": "1"}, {"id": "2"}]}
    verdicts = [*listed, _verdict("q/1", eligible=False), _verdict("q/2", eligible=False)]
    post = _good_tool_gateway(blocked=frozenset({"q/1", "q/2"}))
    quality = acceptance_toolcheck.measure(
        listed, verdicts, synced=synced, cooling=set(), post=post, seed="s"
    )
    assert quality["precision"] == 1.0
    assert quality["recall_estimate"] is None


# -- the hourly systemd unit --------------------------------------------------


def test_acceptance_unit_runs_from_the_installed_checkout_hourly(tmp_path: Path) -> None:
    from verdict import acceptance_service

    calls: list[list[str]] = []
    report = acceptance_service.manage_service(unit_dir=tmp_path, dry_run=True, run=calls.append)
    service = report["files"][acceptance_service.SERVICE]
    timer = report["files"][acceptance_service.TIMER]
    root = Path(acceptance_service.__file__).resolve().parent.parent
    assert f"WorkingDirectory={root}\n" in service
    assert "-m verdict acceptance run --notify dry-run\n" in service
    assert f"SuccessExitStatus={acceptance.RED_EXIT}\n" in service
    assert "OnCalendar=hourly\n" in timer and "Persistent=true\n" in timer
    # Dry-run never writes or calls systemctl.
    assert calls == [] and list(tmp_path.iterdir()) == []


def test_acceptance_unit_install_writes_enables_and_uninstall_removes(tmp_path: Path) -> None:
    from verdict import acceptance_service

    calls: list[list[str]] = []
    acceptance_service.manage_service(unit_dir=tmp_path, run=calls.append)
    assert (tmp_path / acceptance_service.SERVICE).is_file()
    assert ["systemctl", "--user", "enable", "--now", acceptance_service.TIMER] in calls
    # A long oneshot is started without blocking the install.
    assert ["systemctl", "--user", "start", "--no-block", acceptance_service.SERVICE] in calls
    calls.clear()
    acceptance_service.manage_service(unit_dir=tmp_path, uninstall=True, run=calls.append)
    assert not (tmp_path / acceptance_service.SERVICE).exists()
    assert calls[0][:4] == ["systemctl", "--user", "disable", "--now"]


def test_install_service_cli_dry_run_renders_the_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from verdict import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")
    monkeypatch.setattr(sys, "argv", ["verdict", "acceptance", "install-service", "--dry-run"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert "verdict-acceptance.service" in report["files"]
    assert not (tmp_path / "systemd").exists()


def test_judge_route_accepts_bare_terse_output() -> None:
    passed, reason, detail = acceptance.judge_route(0, "mistral/codestral-latest\n", "")
    assert passed is True
    assert reason == "routed to mistral/codestral-latest"
    assert detail == {"model": "mistral/codestral-latest", "reason": None}

    passed, reason, detail = acceptance.judge_route(0, "nvidia/google/gemma-4-31b-it\n", "")
    assert passed is True
    assert detail["model"] == "nvidia/google/gemma-4-31b-it"


def test_judge_route_accepts_json_payload() -> None:
    passed, reason, detail = acceptance.judge_route(0, json.dumps({"model": "p/a"}), "")
    assert passed is True
    assert reason == "routed to p/a"
    assert detail == {"model": "p/a", "reason": None}

    passed, reason, detail = acceptance.judge_route(
        0, json.dumps({"model": "mistral/codestral-latest", "reason": "fastest"}), ""
    )
    assert passed is True
    assert detail == {"model": "mistral/codestral-latest", "reason": "fastest"}


def test_judge_route_refuses_blank_output() -> None:
    passed, reason, detail = acceptance.judge_route(0, "", "")
    assert passed is False
    assert "no route" in reason
    assert detail["model"] is None

    passed, reason, detail = acceptance.judge_route(0, "   \n\n  ", "")
    assert passed is False
    assert "no route" in reason


def test_judge_route_refuses_no_eligible_target_and_nulls() -> None:
    passed, reason, detail = acceptance.judge_route(0, "no_eligible_target\n", "")
    assert passed is False
    assert "no route" in reason

    passed, reason, detail = acceptance.judge_route(
        0, json.dumps({"model": "no_eligible_target", "reason": "all cooling"}), ""
    )
    assert passed is False
    assert "no route" in reason
    assert detail["reason"] == "all cooling"

    passed, reason, detail = acceptance.judge_route(0, "None\n", "")
    assert passed is False

    passed, reason, detail = acceptance.judge_route(0, json.dumps({"model": None}), "")
    assert passed is False


def test_judge_route_refuses_nonzero_exit() -> None:
    passed, reason, detail = acceptance.judge_route(
        1, json.dumps({"model": "no_eligible_target", "reason": "OmniRoute rejected API key"}), ""
    )
    assert passed is False
    assert "exit 1" in reason
    assert detail["reason"] == "OmniRoute rejected API key"

    passed, reason, detail = acceptance.judge_route(1, "mistral/codestral-latest", "error")
    assert passed is False
    assert "exit 1" in reason


def test_judge_route_refuses_malformed_json() -> None:
    passed, reason, detail = acceptance.judge_route(0, '{"model": ', "")
    assert passed is False
    assert "no route" in reason
    assert detail["reason"] == "malformed JSON"

    passed, reason, detail = acceptance.judge_route(0, "{malformed json", "")
    assert passed is False
    assert "no route" in reason
    assert detail["reason"] == "malformed JSON"


def test_judge_route_refuses_logs_and_random_prose() -> None:
    passed, reason, detail = acceptance.judge_route(
        0, "I think the best route is gpt-4 for this task.", ""
    )
    assert passed is False
    assert "no route" in reason
    assert detail["reason"] == "invalid route name"

    passed, reason, detail = acceptance.judge_route(0, "[INFO] starting routing engine...", "")
    assert passed is False
    assert detail["reason"] == "invalid route name"

    passed, reason, detail = acceptance.judge_route(0, "warning: fallback route triggered", "")
    assert passed is False
    assert detail["reason"] == "invalid route name"

    passed, reason, detail = acceptance.judge_route(0, "error: gateway connection refused", "")
    assert passed is False
    assert detail["reason"] == "invalid route name"
