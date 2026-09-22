import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _write_controller_decision(path: Path, *, provider: str = "test", model: str = "exact", reasoning=None) -> Path:
    """Persisted authoritative BOD-104 decision + receipt ref for supervisor tests."""
    now = datetime.now(timezone.utc)
    payload = {
        "execution_path_decision_digest": "bod104-test",
        "selected_upstream_route": f"{provider}/{model}",
        "prime_target": {
            "upstream_provider": provider,
            "upstream_model": model,
            "prime_provider": provider,
            "prime_model": model,
            "binding_digest": "binding-test",
            "reasoning_effort": reasoning,
            "binding_evidence_ref": "evidence://binding-test",
        },
        "context_plan_digest": "ctx-plan",
        "context_pack_digest": "ctx-pack",
        "context_receipt_digest": "ctx-receipt",
        "selected_prompt_digest": "prompt",
        "routing_receipt_ref": "receipt://pre-launch-test",
        "pool_ref": "pool://test",
        "evidence_refs": ["evidence://live"],
        "constituent_freshness": {"inventory": now.isoformat()},
        "minimum_expiry": (now + timedelta(hours=1)).isoformat(),
        "session_decision": "NEW",
        "why_selected": "test fixture",
        "task_profile_digest": "tp",
        "task_slice_digest": "ts",
        "trajectory_digest": "tr",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def module():
    path = ROOT / "scripts/prime_supervisor.py"
    assert path.exists(), "external supervisor missing"
    spec = importlib.util.spec_from_file_location("prime_supervisor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_heartbeat_does_not_reset_progress_deadline():
    m = module()
    assert (
        m.stall_reason(now=601, started=0, progress=0, idle_seconds=600, timeout=3600)
        == "NO_PROGRESS"
    )
    assert (
        m.stall_reason(now=601, started=0, progress=590, idle_seconds=600, timeout=600)
        == "DEADLINE"
    )
    assert m.stall_reason(now=20, started=0, progress=10, idle_seconds=600, timeout=3600) is None


def test_real_process_is_killed_and_reaped(tmp_path):
    m = module()
    result = m.run_attempt(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        tmp_path,
        tmp_path / "out.log",
        lambda: "unchanged",
        0.1,
        2,
        0.02,
    )
    assert result["reason"] == "NO_PROGRESS"
    assert result["returncode"] is not None


def test_run_attempt_admits_identity_before_watchdog(tmp_path):
    m = module()
    events = []

    def fingerprint():
        events.append("fingerprint")
        return "unchanged"

    def on_started(process):
        assert process.poll() is None
        events.append("identity")

    result = m.run_attempt(
        [sys.executable, "-c", "print('finished')"],
        tmp_path,
        tmp_path / "out.log",
        fingerprint,
        5,
        5,
        0.02,
        on_started=on_started,
    )
    assert result == {"reason": "EXIT", "returncode": 0}
    assert events[0] == "identity"


def test_run_attempt_callback_failure_stops_and_reaps_process(tmp_path):
    m = module()
    started = []
    callback_error = RuntimeError("identity mismatch")

    def on_started(process):
        started.append(process)
        raise callback_error

    with pytest.raises(RuntimeError, match="identity mismatch"):
        m.run_attempt(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tmp_path,
            tmp_path / "out.log",
            lambda: pytest.fail("watchdog must not start before identity admission"),
            5,
            5,
            0.02,
            on_started=on_started,
        )
    assert started
    assert started[0].poll() is not None


def test_main_identity_mismatch_skips_watchdog_and_blocks(tmp_path, monkeypatch):
    """Deterministic fence: verify failure before fingerprint; recover never sees a result."""
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    decision = _write_controller_decision(state / "d.json")
    events = []

    real_run_attempt = m.run_attempt

    def wrapped_run_attempt(*args, **kwargs):
        on_started = kwargs.get("on_started")
        fingerprint = args[3] if len(args) > 3 else kwargs.get("fingerprint")

        def gated_fingerprint():
            events.append("fingerprint")
            return fingerprint()

        def gated_on_started(process):
            events.append("on_started")
            if on_started is not None:
                on_started(process)

        kwargs = dict(kwargs)
        kwargs["on_started"] = gated_on_started
        new_args = list(args)
        if len(new_args) > 3:
            new_args[3] = gated_fingerprint
        else:
            kwargs["fingerprint"] = gated_fingerprint
        return real_run_attempt(*new_args, **kwargs)

    def boom(**kwargs):
        events.append("verify")
        raise m.ControllerLaunchError("identity_mismatch", "model observed='wrong' expected='exact'")

    monkeypatch.setattr(m, "run_attempt", wrapped_run_attempt)
    monkeypatch.setattr(m, "verify_startup_controller_identity", boom)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: events.append("stop_daemon"))
    monkeypatch.setattr(
        m.subprocess,
        "Popen",
        lambda *a, **k: type("P", (), {"pid": 1, "poll": lambda self: None, "returncode": None, "wait": lambda self, timeout=None: 0})(),
    )
    monkeypatch.setattr(m, "stop_group", lambda process: events.append("stop_group"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            "prime-agent",
            "--controller-decision",
            str(decision),
            "--provider",
            "test",
            "--model",
            "exact",
            "--max-restarts",
            "0",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
        ],
    )
    assert m.main() == 2
    assert events[0] == "on_started"
    assert "verify" in events
    assert "fingerprint" not in events
    assert "stop_group" in events
    assert "stop_daemon" in events
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["reason_code"] == "identity_mismatch"
    assert "result" not in checkpoint


def test_skip_identity_verify_requires_test_mode(monkeypatch):
    m = module()
    monkeypatch.delenv("VERDICT_TEST_MODE", raising=False)
    monkeypatch.setattr(sys, "argv", ["prime_supervisor.py", "--skip-identity-verify"])
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert exc.value.code == 2


def test_cli_identity_mismatch_blocks_before_mission_watchdog(tmp_path):
    """Popen is required for roster observation; mission/watchdog must not run after mismatch.

    The child may start, but delayed mission work must be killed by the admission
    fence before it lands. ``prime list`` itself must never write the mission marker.
    """
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    fake = tmp_path / "prime-mismatch"
    # Delay "mission" marker so identity mismatch + stop_group win the race.
    # Immediate marker writes race Popen body vs admit_started and flake.
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "args=sys.argv\n"
        "session=Path(args[args.index('--session-dir')+1]) if '--session-dir' in args else next(Path(" + repr(str(state / 'sessions')) + ").glob('*'))\n"
        f"marker=Path({str(tmp_path / 'stopped')!r})\n"
        "if 'list' in args:\n"
        "  # Roster-only path: never touch mission markers.\n"
        "  if marker.exists(): print(json.dumps({'sessions':[]}))\n"
        "  else: print(json.dumps({'sessions':[{'id':'root','sessionFile':str(session / 'root.jsonl'),'lifecycle':'live','runtimeKind':'top-level','rlmDepth':0,'provider':'test','model':'wrong'}]}))\n"
        "  raise SystemExit(0)\n"
        "if 'stop' in args:\n"
        "  marker.write_text('stopped')\n"
        "  raise SystemExit(0)\n"
        "time.sleep(15)\n"
        "(session / 'mission-started').write_text('started')\n"
        "time.sleep(30)\n"
    )
    fake.chmod(0o700)
    decision = _write_controller_decision(state / "controller_decision.json")
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo", str(repo), "--state-dir", str(state), "--prime", str(fake),
            "--controller-decision", str(decision), "--provider", "test", "--model", "exact",
            "--identity-timeout", "1", "--idle-seconds", "30", "--timeout", "60",
            "--poll-seconds", "5", "--max-restarts", "0",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 2, result.stdout + result.stderr
    # Must not wait out mission timeout / first idle poll; identity fails closed immediately.
    assert elapsed < 10, f"waited too long for identity fence: {elapsed}"
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["reason_code"] == "identity_mismatch"
    assert "result" not in checkpoint  # recover/watchdog never recorded an attempt result
    session_dirs = list((state / "sessions").iterdir())
    assert session_dirs
    # Child reached Popen (session dir exists) but delayed mission work never landed.
    assert not (session_dirs[0] / "mission-started").exists()


def test_fast_worker_exit(tmp_path):
    m = module()
    result = m.run_attempt(
        [sys.executable, "-c", "print('finished')"],
        tmp_path,
        tmp_path / "out.log",
        lambda: "unchanged",
        5,
        5,
        0.02,
    )
    assert result == {"reason": "EXIT", "returncode": 0}


def test_common_lock_excludes_second_supervisor(tmp_path):
    m = module()
    with m.acquire_lock(tmp_path / "lock"):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)",
                str(tmp_path / "lock"),
            ],
            capture_output=True,
        )
        assert result.returncode != 0


def test_repeated_failure_has_bounded_recovery(tmp_path):
    m = module()
    calls = []

    def attempt():
        calls.append(1)
        return {"reason": "NO_PROGRESS", "returncode": -15}

    assert m.recover(attempt, tmp_path, max_restarts=2) == 2
    assert len(calls) == 3
    checkpoint = json.loads((tmp_path / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["attempts"] == 3


def test_owned_daemon_selection_never_stops_unrelated_session(tmp_path):
    m = module()
    roster = {
        "sessions": [
            {"id": "owned", "sessionFile": str(tmp_path / "attempt/a.jsonl"), "lifecycle": "live"},
            {"id": "other", "sessionFile": str(tmp_path / "other/a.jsonl"), "lifecycle": "live"},
            {
                "id": "stopped",
                "sessionFile": str(tmp_path / "attempt/b.jsonl"),
                "lifecycle": "saved",
            },
        ]
    }
    assert [s["id"] for s in m.owned_sessions(roster, tmp_path / "attempt")] == ["owned"]


@pytest.mark.parametrize(
    "roster", [{}, {"sessions": None}, {"sessions": [{"lifecycle": "unknown"}]}]
)
def test_unknown_roster_blocks_cleanup_confirmation(tmp_path, roster):
    with pytest.raises(ValueError):
        module().owned_sessions(roster, tmp_path)


def test_daemon_cleanup_stops_only_own_id_and_confirms_absence(tmp_path, monkeypatch):
    m = module()
    responses = iter(
        [
            {
                "sessions": [
                    {"id": "own", "sessionFile": str(tmp_path / "a.jsonl"), "lifecycle": "live"},
                    {"id": "other", "sessionFile": "/elsewhere/session.jsonl", "lifecycle": "live"},
                ]
            },
            {
                "sessions": [
                    {"id": "other", "sessionFile": "/elsewhere/session.jsonl", "lifecycle": "live"}
                ]
            },
        ]
    )
    calls = []
    monkeypatch.setattr(m.subprocess, "check_output", lambda *a, **k: json.dumps(next(responses)))
    monkeypatch.setattr(m.subprocess, "run", lambda command, **k: calls.append(command))
    m.stop_owned_daemon("prime-agent", tmp_path)
    assert calls == [["prime-agent", "stop", "own"]]


def test_sigterm_reaps_worker_and_persists_blocker(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "baseline",
        ],
        check=True,
    )
    fake = tmp_path / "prime"
    pid_file = tmp_path / "child.pid"
    fake.write_text(
        f"#!{sys.executable}\nimport sys,time,os\nfrom pathlib import Path\n"
        "if 'list' in sys.argv:\n print('{\"sessions\":[]}')\n sys.exit(0)\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n"
    )
    fake.chmod(0o700)
    decision = _write_controller_decision(tmp_path / "controller_decision.json")
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--prime",
            str(fake),
            "--controller-decision",
            str(decision),
            "--provider",
            "test",
            "--model",
            "exact",
            "--skip-identity-verify",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
        ],
        env={**os.environ, "VERDICT_TEST_MODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                out, err = process.communicate(timeout=2)
                raise AssertionError(
                    f"supervisor exited before worker launch: code={process.returncode}\n"
                    f"stdout={out!r}\nstderr={err!r}"
                )
            time.sleep(0.02)
        assert pid_file.exists(), "worker never launched"
        # Wait for non-empty pid write (avoid create-vs-flush race).
        content = ""
        flush_deadline = time.monotonic() + 2
        while time.monotonic() < flush_deadline:
            content = pid_file.read_text().strip()
            if content:
                break
            time.sleep(0.01)
        assert content, "worker pid file remained empty"
        child_pid = int(content)
        process.terminate()
        process.communicate(timeout=10)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        checkpoint = json.loads((repo / ".git/verdict-prime/supervisor.json").read_text())
        assert checkpoint["status"] == "BLOCKED"
        assert "interrupted" in checkpoint["reason"]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def workflow_module():
    return _load("prime_state_hardening", ROOT / "scripts/prime_state.py")


def _git_init(repo):
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@e.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "base",
        ],
        check=True,
    )


def test_fingerprint_ignores_unregistered_evidence_churn(tmp_path):
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    issue_ev = state / "issues/BOD-12/evidence"
    issue_ev.mkdir(parents=True)
    (state / "issues/BOD-12/receipt.json").write_text(
        json.dumps(
            {
                "status": "IMPLEMENTING",
                "current_step": "write code",
                "last_progress_at": "t0",
                "dispatch_id": "d1",
                "commands": [{"artifact": "evidence/unit.log"}],
            }
        )
    )
    (issue_ev / "unit.log").write_text("real\n")
    first = m.workspace_fingerprint(repo, state)
    # A hung worker spamming new unregistered evidence files is not progress.
    for i in range(5):
        (issue_ev / f"noise-{i}.log").write_text("churn\n")
    second = m.workspace_fingerprint(repo, state)
    assert m.progress_made(first, second) is False
    # Unregistered repository logs are also noise, not source progress.
    (repo / "worker-noise.log").write_text("churn\n")
    noise = m.workspace_fingerprint(repo, state)
    assert m.progress_made(second, noise) is False
    # A symlink must not make its target look like a source edit.
    (repo / "secret.txt").write_text("secret\n")
    (repo / "secret.py").symlink_to(repo / "secret.txt")
    linked = m.workspace_fingerprint(repo, state)
    assert m.progress_made(noise, linked) is False
    # A new substantive source change is progress.
    (repo / "worker.py").write_text("work\n")
    third = m.workspace_fingerprint(repo, state)
    assert m.progress_made(second, third) is True
    # A receipt step transition is progress even with unchanged source.
    (state / "issues/BOD-12/receipt.json").write_text(
        json.dumps(
            {
                "status": "IMPLEMENTING",
                "current_step": "ran tests",
                "last_progress_at": "t1",
                "dispatch_id": "d1",
                "commands": [{"artifact": "evidence/unit.log"}],
            }
        )
    )
    fourth = m.workspace_fingerprint(repo, state)
    assert m.progress_made(third, fourth) is True


def test_supervisor_reaps_stale_lease_without_manual_kill(tmp_path):
    wf = workflow_module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    # A four-hour-old writer that never progressed.
    wf.acquire_lease(
        state,
        issue="BOD-12",
        dispatch_id="old",
        worker_id="w-old",
        worktree=str(repo),
        branch="b",
        stale_after_seconds=600,
        supervisor_id="dead-supervisor",
        now=time.time() - 4 * 3600,
    )
    fake = tmp_path / "prime"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'list' in sys.argv:\n    print('{\"sessions\":[]}')\n    sys.exit(0)\n"
        "print('ran')\n"
    )
    fake.chmod(0o700)
    decision = _write_controller_decision(state / "controller_decision.json")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            str(fake),
            "--controller-decision",
            str(decision),
            "--provider",
            "test",
            "--model",
            "exact",
            "--skip-identity-verify",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
            "--max-restarts",
            "0",
        ],
        env={**os.environ, "VERDICT_TEST_MODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert wf.read_lease(state, "BOD-12")["status"] == "SUPERSEDED", result.stdout + result.stderr
    assert "BOD-12" in result.stdout


def test_state_dir_override_is_honored(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "custom-state"
    fake = tmp_path / "prime"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'list' in sys.argv:\n    print('{\"sessions\":[]}')\n    sys.exit(0)\n"
        "print('ran')\n"
    )
    fake.chmod(0o700)
    decision = _write_controller_decision(state / "controller_decision.json")
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            str(fake),
            "--controller-decision",
            str(decision),
            "--provider",
            "test",
            "--model",
            "exact",
            "--skip-identity-verify",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
            "--max-restarts",
            "0",
        ],
        env={**os.environ, "VERDICT_TEST_MODE": "1"},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert (state / "supervisor.json").exists()
    assert not (repo / ".git/verdict-prime").exists()


def test_cli_worker_dies_and_recovery_is_bounded(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    fake = tmp_path / "prime-dies"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'list' in sys.argv:\n print('{\"sessions\":[]}')\n sys.exit(0)\n"
        "sys.exit(17)\n"
    )
    fake.chmod(0o700)
    decision = _write_controller_decision(state / "controller_decision.json")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            str(fake),
            "--controller-decision",
            str(decision),
            "--provider",
            "test",
            "--model",
            "exact",
            "--skip-identity-verify",
            "--idle-seconds",
            "1",
            "--timeout",
            "5",
            "--poll-seconds",
            ".02",
            "--max-restarts",
            "1",
        ],
        env={**os.environ, "VERDICT_TEST_MODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["attempts"] == 2
    assert checkpoint["result"]["reason"] == "EXIT"


def test_cli_alive_hang_is_stall_not_healthy(tmp_path):
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    fake = tmp_path / "prime-hangs"
    fake.write_text(
        f"#!{sys.executable}\nimport sys,time\n"
        "if 'list' in sys.argv:\n print('{\"sessions\":[]}')\n sys.exit(0)\n"
        "time.sleep(60)\n"
    )
    fake.chmod(0o700)
    decision = _write_controller_decision(state / "controller_decision.json")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            str(fake),
            "--controller-decision",
            str(decision),
            "--provider",
            "test",
            "--model",
            "exact",
            "--skip-identity-verify",
            "--idle-seconds",
            ".05",
            "--timeout",
            "5",
            "--poll-seconds",
            ".02",
            "--max-restarts",
            "0",
        ],
        env={**os.environ, "VERDICT_TEST_MODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["result"]["reason"] == "NO_PROGRESS"
    assert checkpoint["result"]["returncode"] is not None


def test_resolve_automatic_without_selector_or_file_blocks(tmp_path):
    m = module()
    with pytest.raises(Exception) as exc:
        m.resolve_controller_decision(
            decision_path=tmp_path / "missing.json",
            mission_path=None,
            attempt_id="a1",
            provider=None,
            model=None,
            thinking=None,
            override_reason=None,
        )
    assert "missing_authoritative_decision" in str(exc.value)


def test_resolve_automatic_uses_selector_double(tmp_path):
    m = module()
    mission_calls: list[object] = []

    class FakeSelector:
        def select_controller_launch(self, mission, *, override=None, session_state=None, now=None):
            mission_calls.append(mission)
            # Build via real assembly helper + fixture persisted decision.
            decision_path = _write_controller_decision(
                tmp_path / "generated.json",
                provider="omniroute",
                model="gc/grok-4.5",
                reasoning="high",
            )
            persisted = m.load_persisted_authoritative_decision(decision_path)
            return m.decide_controller_launch(mission, persisted=persisted, override=override, now=now)

    decision = m.resolve_controller_decision(
        decision_path=None,
        mission_path=None,
        attempt_id="attempt-sel",
        provider=None,
        model=None,
        thinking=None,
        override_reason=None,
        selector=FakeSelector(),
    )
    assert mission_calls
    assert decision.mode == "automatic"
    assert decision.prime_target.prime_provider == "omniroute"
    assert decision.prime_target.prime_model == "gc/grok-4.5"
    cmd = m.build_supervisor_prime_command(
        prime="prime-agent",
        repo=tmp_path,
        session_dir=tmp_path / "session",
        decision=decision,
        prompt="go",
    )
    assert cmd[cmd.index("--provider") + 1] == "omniroute"
    assert cmd[cmd.index("--model") + 1] == "gc/grok-4.5"
    assert "--thinking" in cmd


def test_resolve_incomplete_override_fails(tmp_path):
    m = module()
    decision = _write_controller_decision(tmp_path / "d.json")
    with pytest.raises(Exception) as exc:
        m.resolve_controller_decision(
            decision_path=decision,
            mission_path=None,
            attempt_id="a1",
            provider="test",
            model=None,
            thinking=None,
            override_reason=None,
        )
    assert "incomplete_override" in str(exc.value)


def test_build_supervisor_command_uses_exact_decision_identity(tmp_path):
    m = module()
    decision_path = _write_controller_decision(
        tmp_path / "d.json", provider="omniroute", model="gc/grok-4.5", reasoning="high"
    )
    decision = m.resolve_controller_decision(
        decision_path=decision_path,
        mission_path=None,
        attempt_id="attempt-xyz",
        provider=None,
        model=None,
        thinking=None,
        override_reason=None,
    )
    assert decision.mode == "automatic"
    assert decision.routing_receipt_ref == "receipt://pre-launch-test"
    cmd = m.build_supervisor_prime_command(
        prime="prime-agent",
        repo=tmp_path,
        session_dir=tmp_path / "session",
        decision=decision,
        prompt="go",
    )
    assert cmd[cmd.index("--provider") + 1] == "omniroute"
    assert cmd[cmd.index("--model") + 1] == "gc/grok-4.5"
    assert cmd[cmd.index("--thinking") + 1] == "high"
    assert "auto/" not in " ".join(cmd)


def test_override_provenance_recorded(tmp_path):
    m = module()
    decision_path = _write_controller_decision(
        tmp_path / "d.json", provider="omniroute", model="gc/grok-4.5", reasoning="high"
    )
    decision = m.resolve_controller_decision(
        decision_path=decision_path,
        mission_path=None,
        attempt_id="attempt-1",
        provider="omniroute",
        model="gc/grok-4.5",
        thinking="high",
        override_reason="operator pin",
    )
    assert decision.mode == "override"
    assert decision.override_provenance["source"] == "cli"
    assert decision.override_provenance["reason"] == "operator pin"


def test_missing_decision_blocks_cli_launch(tmp_path):
    """Bare automatic CLI without injectable wiring fails closed (no launch)."""
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    # Ensure no implicit decision file can be picked up.
    assert not (state / "controller_decision.json").exists()
    fake = tmp_path / "prime"
    fake.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "if 'list' in sys.argv:\n print('{\"sessions\":[]}')\n sys.exit(0)\n"
        "print('should-not-run')\n"
    )
    fake.chmod(0o700)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            str(fake),
            "--skip-identity-verify",
            "--idle-seconds",
            "1",
            "--timeout",
            "5",
            "--max-restarts",
            "0",
        ],
        env={**os.environ, "VERDICT_TEST_MODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    # Production factory is attempted first; unavailable config fails closed.
    assert (
        "production_factory_unavailable" in combined
        or "missing_authoritative_decision" in combined
    )
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint.get("reason_code") in {
        "production_factory_unavailable",
        "missing_authoritative_decision",
        None,
    } or "production_factory_unavailable" in checkpoint.get("reason", "")



def test_automatic_cli_path_uses_module_selector_and_exact_argv(tmp_path, monkeypatch):
    """Automatic CLI (no --provider/--model) uses CONTROLLER_SELECTOR double."""
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    launched: list[list[str]] = []

    class FakeSelector:
        def select_controller_launch(self, mission, *, override=None, session_state=None, now=None):
            decision_path = _write_controller_decision(
                tmp_path / "sel.json",
                provider="omniroute",
                model="gc/selected",
                reasoning=None,
            )
            persisted = m.load_persisted_authoritative_decision(decision_path)
            return m.decide_controller_launch(mission, persisted=persisted, override=override, now=now)

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", FakeSelector())

    def fake_run_attempt(command, repo, log_path, fingerprint, idle, timeout, **kwargs):
        launched.append(list(command))
        return {
            "status": "DONE",
            "returncode": 0,
            "reason": "OK",
            "duration": 0.01,
            "fingerprint": "fp",
        }

    monkeypatch.setattr(m, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(
        m,
        "verify_startup_controller_identity",
        lambda **kwargs: {
            "session_id": "s1",
            "provider": "omniroute",
            "model": "gc/selected",
            "thinking_level": None,
        },
    )

    # Drive a single attempt through resolve + command build without full main().
    decision = m.resolve_controller_decision(
        decision_path=None,
        mission_path=None,
        attempt_id="cli-1",
        provider=None,
        model=None,
        thinking=None,
        override_reason=None,
        selector=m.CONTROLLER_SELECTOR,
    )
    cmd = m.build_supervisor_prime_command(
        prime="prime-agent",
        repo=repo,
        session_dir=state / "sessions" / "cli-1",
        decision=decision,
        prompt="go",
    )
    assert cmd[cmd.index("--provider") + 1] == "omniroute"
    assert cmd[cmd.index("--model") + 1] == "gc/selected"
    assert "--thinking" not in cmd

    # No selector result => no subprocess launch.
    class BlockingSelector:
        def select_controller_launch(self, mission, *, override=None, session_state=None, now=None):
            raise m.ControllerLaunchError("no_eligible_route", "nothing qualifies")

    with pytest.raises(m.ControllerLaunchError) as exc:
        m.resolve_controller_decision(
            decision_path=None,
            mission_path=None,
            attempt_id="cli-2",
            provider=None,
            model=None,
            thinking=None,
            override_reason=None,
            selector=BlockingSelector(),
        )
    assert exc.value.reason_code == "no_eligible_route"


def test_automatic_cli_uses_production_factory_and_compiled_prompt_digest(tmp_path, monkeypatch):
    """Default automatic wiring invokes factory; argv prompt sha256 matches decision."""
    import hashlib
    from types import SimpleNamespace

    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    factory_calls: list[dict] = []
    launched: list[list[str]] = []

    compiled_prompt = (
        "COMPILED-CONTROLLER-PROMPT\n"
        "/skill:verdict-resume\n"
        "exact bytes for digest proof\n"
    )
    prompt_digest = "sha256:" + hashlib.sha256(compiled_prompt.encode("utf-8")).hexdigest()

    decision_path = _write_controller_decision(
        tmp_path / "factory-decision.json",
        provider="omniroute",
        model="gc/selected",
        reasoning=None,
    )
    # Overwrite selected_prompt_digest to match compiled bytes.
    payload = json.loads(decision_path.read_text())
    payload["selected_prompt_digest"] = prompt_digest
    decision_path.write_text(json.dumps(payload, indent=2) + "\n")

    class FakeHooks:
        pass

    class FakeArtifacts:
        def __init__(self):
            self.last_compiled = SimpleNamespace(
                route_id="omniroute/gc/selected",
                plan_digest="plan",
                pack_digest="pack",
                receipt_digest="receipt",
                prompt_digest=prompt_digest,
                compiled_prompt=compiled_prompt,
            )

    class FakeBundle:
        def __init__(self):
            self.hooks = FakeHooks()
            self.artifacts = FakeArtifacts()

    def fake_factory(**kwargs):
        factory_calls.append(dict(kwargs))
        return FakeBundle()

    def fake_resolve(**kwargs):
        assert kwargs.get("selection_hooks") is not None
        assert kwargs.get("selector") is None
        persisted = m.load_persisted_authoritative_decision(decision_path)
        mission = m.load_controller_mission(None, attempt_id=kwargs["attempt_id"])
        return m.decide_controller_launch(mission, persisted=persisted, now=kwargs.get("now"))

    def fake_run_attempt(command, *args, **kwargs):
        launched.append(list(command))
        # Extract token from session-dir and write a valid outcome.
        session_dir = Path(command[command.index("--session-dir") + 1])
        token = session_dir.name
        (state / "outcome.json").write_text(
            json.dumps({"run_id": token, "status": "DONE", "reason": "ok"}) + "\n"
        )
        return {"reason": "EXIT", "returncode": 0}

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", fake_factory)
    monkeypatch.setattr(m, "resolve_controller_decision", fake_resolve)
    monkeypatch.setattr(m, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setattr(m, "verify_startup_controller_identity", lambda **k: {
        "session_id": "s1",
        "provider": "omniroute",
        "model": "gc/selected",
        "thinking_level": None,
    })
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo", str(repo),
            "--state-dir", str(state),
            "--prime", "prime-agent",
            "--skip-identity-verify",
            "--max-restarts", "0",
            "--idle-seconds", "30",
            "--timeout", "60",
        ],
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    assert m.main() == 0
    assert factory_calls, "production factory must be invoked for bare automatic mode"
    assert launched, "prime command must be launched"
    cmd = launched[0]
    prompt = cmd[-1]
    assert prompt == compiled_prompt
    actual = "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert actual == prompt_digest
    run = json.loads((state / "run.json").read_text())
    assert run["selected_prompt_digest"] == prompt_digest
    assert run["prime_model"] == "gc/selected"


def test_production_factory_unavailable_blocks_automatic_cli(tmp_path, monkeypatch):
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    launched: list[list[str]] = []

    def boom_factory(**kwargs):
        raise m.ControllerLaunchError(
            "production_factory_unavailable",
            "no trusted prime-target-map.json found",
        )

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", boom_factory)
    monkeypatch.setattr(
        m,
        "run_attempt",
        lambda *a, **k: launched.append(list(a[0])) or {"reason": "EXIT", "returncode": 0},
    )
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo", str(repo),
            "--state-dir", str(state),
            "--prime", "prime-agent",
            "--skip-identity-verify",
            "--max-restarts", "0",
            "--idle-seconds", "30",
            "--timeout", "60",
        ],
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    assert m.main() == 2
    assert launched == []
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint.get("reason_code") == "production_factory_unavailable" or (
        "production_factory_unavailable" in checkpoint.get("reason", "")
    )


def test_injected_selector_overrides_production_factory(tmp_path, monkeypatch):
    """CONTROLLER_SELECTOR injection is preserved and skips the production factory."""
    m = module()
    factory_calls: list[object] = []

    class FakeSelector:
        def select_controller_launch(self, mission, *, override=None, session_state=None, now=None):
            decision_path = _write_controller_decision(
                tmp_path / "sel.json", provider="omniroute", model="gc/injected"
            )
            persisted = m.load_persisted_authoritative_decision(decision_path)
            return m.decide_controller_launch(mission, persisted=persisted, override=override, now=now)

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", FakeSelector())
    monkeypatch.setattr(
        m,
        "CONTROLLER_SELECTION_FACTORY",
        lambda **kwargs: factory_calls.append(kwargs) or (_ for _ in ()).throw(
            AssertionError("factory must not run when selector injected")
        ),
    )
    decision = m.resolve_controller_decision(
        decision_path=None,
        mission_path=None,
        attempt_id="inj-1",
        provider=None,
        model=None,
        thinking=None,
        override_reason=None,
        selector=m.CONTROLLER_SELECTOR,
        selection_hooks=None,
    )
    assert decision.prime_target.prime_model == "gc/injected"
    assert factory_calls == []


def test_never_implicitly_loads_state_controller_decision_json(tmp_path, monkeypatch):
    """state/controller_decision.json must not be used without --controller-decision."""
    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    # Plant a tempting implicit file.
    _write_controller_decision(state / "controller_decision.json", provider="trap", model="implicit")

    def boom_factory(**kwargs):
        raise m.ControllerLaunchError("production_factory_unavailable", "forced")

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", boom_factory)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo", str(repo),
            "--state-dir", str(state),
            "--prime", "prime-agent",
            "--skip-identity-verify",
            "--max-restarts", "0",
            "--idle-seconds", "30",
            "--timeout", "60",
        ],
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    assert m.main() == 2
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    # Must not have launched with the trap identity.
    run_path = state / "run.json"
    if run_path.exists():
        run = json.loads(run_path.read_text())
        assert run.get("prime_model") != "implicit"


def _evidence_backed_offer(
    *,
    route_id: str,
    provider: str,
    model: str,
    cash_usd: str,
    capability_tier: int = 2,
    when: datetime | None = None,
):
    """Minimal evidence-backed ExecutionPathOffer for BOD-119 factory tests."""
    from decimal import Decimal

    from verdict.cost_ledger import CostTerm
    from verdict.effective_capability import (
        AssistanceCost,
        AssistancePlan,
        DecompositionRequirement,
        ProvenanceClaim,
        TaskSlice,
        VerificationStrategy,
    )
    from verdict.execution_path import ExecutionPathOffer
    from verdict.expected_cost import ExpectedStrategyCost
    from verdict.session_economics import ConcreteRoute

    observed = when or datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    route = ConcreteRoute(
        route_id=route_id,
        gateway="omniroute",
        provider=provider,
        model=model,
        credential_pool=None,
        capability_tier=capability_tier,
        eligible=True,
        excluded=False,
    )
    assistance = AssistanceCost(
        context_tokens=100,
        tool_tokens=10,
        planning_tokens=0,
        verification_tokens=50,
    )
    plan = AssistancePlan(
        plan_id=f"seed:{route_id}",
        candidate_id=route_id,
        task_slice=TaskSlice(
            slice_id="attempt",
            objective="test",
            acceptance_criteria=("test",),
            proof_criteria=("controller-launch-proof",),
        ),
        required_intrinsic_capabilities=(),
        required_context_slots=("instructions", "state"),
        required_context_evidence=("proof",),
        required_tool_capabilities=(),
        selected_tool_surface=(),
        decomposition=DecompositionRequirement(required=False),
        verification=VerificationStrategy(
            kind="controller_proof",
            proof_criteria=("controller-launch-proof",),
        ),
        assistance_cost=assistance,
        result="sufficient",
        reasons=("test_evidence",),
        intrinsic_sufficient=True,
        assisted_sufficient=True,
        assistance_delta=(),
        provenance=(
            ProvenanceClaim(
                kind="passport",
                claim=route_id,
                source="test",
                digest="sha256:test",
                observed_at=observed.isoformat(),
                freshness="fresh",
                fresh=True,
            ),
        ),
        evidence_digest="sha256:test",
        context_plan_requirements={
            "candidate_id": route_id,
            "required_slot_types": ["instructions", "state"],
        },
    )
    expected = ExpectedStrategyCost.build(
        strategy_id=f"direct_cheap:{route_id}",
        trajectory_id="traj-test",
        terms=(
            CostTerm(
                kind="execution",
                amount=Decimal(cash_usd),
                unit="usd",
                status="estimated",
                observed_at=observed,
            ),
        ),
        is_free=Decimal(cash_usd) == 0,
        qualified=True,
    )
    return ExecutionPathOffer(
        strategy="direct_cheap",
        route=route,
        assistance_plan=plan,
        expected_cost=expected,
        is_cheap=Decimal(cash_usd) == 0,
        is_paid=Decimal(cash_usd) != 0,
    )


def test_production_factory_supplies_session_factories_without_prior_session(tmp_path, monkeypatch):
    """Bare automatic success path: no prior SessionState, real factory double."""
    from types import SimpleNamespace

    m = module()
    repo = tmp_path / "repo"
    _git_init(repo)
    state = tmp_path / "state"
    state.mkdir()
    factory_calls: list[dict] = []
    launched: list[list[str]] = []

    compiled_prompt = "COMPILED-NO-PRIOR\nexact digest bytes\n"
    import hashlib

    prompt_digest = "sha256:" + hashlib.sha256(compiled_prompt.encode("utf-8")).hexdigest()
    decision_path = _write_controller_decision(
        tmp_path / "auto-decision.json",
        provider="omniroute",
        model="gc/selected",
        reasoning=None,
    )
    payload = json.loads(decision_path.read_text())
    payload["selected_prompt_digest"] = prompt_digest
    decision_path.write_text(json.dumps(payload, indent=2) + "\n")

    offer = _evidence_backed_offer(
        route_id="omniroute/gc/selected",
        provider="omniroute",
        model="gc/selected",
        cash_usd="0.12",
    )
    cost_calls: list[tuple] = []
    task_calls: list[object] = []

    def seed_offers(mission, when):
        return (offer,)

    # Build real BOD-119 factories through the supervisor helper under test.
    cost_factory, task_factory, wrapped_seed = m._build_bod119_session_factories(
        seed_offers=seed_offers
    )

    def tracking_cost(current, fresh, when):
        cost_calls.append((current.route_id, fresh.route_id))
        return cost_factory(current, fresh, when)

    def tracking_task(mission):
        task_calls.append(mission)
        return task_factory(mission)

    class FakeArtifacts:
        def __init__(self):
            self.last_compiled = SimpleNamespace(
                route_id="omniroute/gc/selected",
                plan_digest="plan",
                pack_digest="pack",
                receipt_digest="receipt",
                prompt_digest=prompt_digest,
                compiled_prompt=compiled_prompt,
            )

    class FakeBundle:
        def __init__(self, hooks):
            self.hooks = hooks
            self.artifacts = FakeArtifacts()

    def fake_factory(**kwargs):
        factory_calls.append(dict(kwargs))
        # Real factory double: supply BOD-119 factories derived from evidence-backed
        # seed offers (same contract as build_production_controller_selection_bundle).
        # Do not invent zero prices; reuse supervisor helper under test.
        hooks = SimpleNamespace(
            seed_offers=wrapped_seed,
            cost_state_factory=tracking_cost,
            task_state_factory=tracking_task,
        )
        assert hooks.cost_state_factory is not None
        assert hooks.task_state_factory is not None
        return FakeBundle(hooks)

    def fake_resolve(**kwargs):
        # No prior SessionState — selection must still succeed.
        assert kwargs.get("session_state") is None
        assert kwargs.get("selection_hooks") is not None
        hooks = kwargs["selection_hooks"]
        assert hooks.cost_state_factory is not None
        assert hooks.task_state_factory is not None
        # Exercise task factory (mission requirements) without requiring continuity.
        mission = m.load_controller_mission(None, attempt_id=kwargs["attempt_id"])
        task = hooks.task_state_factory(mission)
        assert task.required_capability_tier >= 1
        task_calls.append(task)
        persisted = m.load_persisted_authoritative_decision(decision_path)
        return m.decide_controller_launch(mission, persisted=persisted, now=kwargs.get("now"))

    def fake_run_attempt(command, *args, **kwargs):
        launched.append(list(command))
        session_dir = Path(command[command.index("--session-dir") + 1])
        token = session_dir.name
        (state / "outcome.json").write_text(
            json.dumps({"run_id": token, "status": "DONE", "reason": "ok"}) + "\n"
        )
        return {"reason": "EXIT", "returncode": 0}

    monkeypatch.setattr(m, "CONTROLLER_SELECTOR", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_HOOKS", None)
    monkeypatch.setattr(m, "CONTROLLER_SESSION_STATE", None)
    monkeypatch.setattr(m, "CONTROLLER_SELECTION_FACTORY", fake_factory)
    monkeypatch.setattr(m, "resolve_controller_decision", fake_resolve)
    monkeypatch.setattr(m, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(m, "stop_owned_daemon", lambda *a, **k: None)
    monkeypatch.setattr(
        m,
        "verify_startup_controller_identity",
        lambda **k: {
            "session_id": "s1",
            "provider": "omniroute",
            "model": "gc/selected",
            "thinking_level": None,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prime_supervisor.py",
            "--repo",
            str(repo),
            "--state-dir",
            str(state),
            "--prime",
            "prime-agent",
            "--skip-identity-verify",
            "--max-restarts",
            "0",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
        ],
    )
    monkeypatch.setenv("VERDICT_TEST_MODE", "1")
    assert m.main() == 0
    assert factory_calls, "production factory must run for bare automatic mode"
    assert launched, "prime must launch without prior SessionState"
    assert task_calls, "task_state_factory must be callable from production hooks"
    cmd = launched[0]
    assert cmd[-1] == compiled_prompt
    actual = "sha256:" + hashlib.sha256(cmd[-1].encode("utf-8")).hexdigest()
    assert actual == prompt_digest


def test_prior_session_uses_valid_factories_or_fails_named(tmp_path):
    """Prior SessionState path must call evidence-backed factories or fail named."""
    m = module()
    from verdict.session_economics import ConcreteRoute, SessionState

    when = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    stay_offer = _evidence_backed_offer(
        route_id="omniroute/gc/current",
        provider="omniroute",
        model="gc/current",
        cash_usd="0.10",
        when=when,
    )
    switch_offer = _evidence_backed_offer(
        route_id="omniroute/gc/fresh",
        provider="omniroute",
        model="gc/fresh",
        cash_usd="0.08",
        when=when,
    )

    def seed_offers(mission, when_):
        return (stay_offer, switch_offer)

    cost_factory, task_factory, wrapped_seed = m._build_bod119_session_factories(
        seed_offers=seed_offers
    )
    assert wrapped_seed is not None
    mission = m.ControllerMission(
        mission_id="m1",
        story_id="s1",
        attempt_id="a1",
        objective="prove session factories",
        orchestration_burden="high",
        proof_burden="frontier",
    )
    # Capture offers via wrapped seed (as selector would).
    offers = wrapped_seed(mission, when)
    assert len(offers) == 2

    task = task_factory(mission)
    assert task.required_capability_tier == 3  # high/frontier burden

    current = stay_offer.route
    fresh = switch_offer.route
    cost = cost_factory(current, fresh, when)
    assert cost.stay_expected.cash_usd is not None
    assert cost.switch_expected.cash_usd is not None
    assert str(cost.stay_expected.cash_usd) == "0.10"
    assert str(cost.switch_expected.cash_usd) == "0.08"
    assert cost.stay_expected.strategy_id.startswith("stay:")
    assert cost.switch_expected.strategy_id.startswith("switch:")

    # Missing evidence for a route must fail named before launch — never invent.
    unknown = ConcreteRoute(
        route_id="omniroute/gc/unknown",
        gateway="omniroute",
        provider="omniroute",
        model="gc/unknown",
        credential_pool=None,
        capability_tier=2,
        eligible=True,
    )
    with pytest.raises(m.ControllerLaunchError) as exc:
        cost_factory(current, unknown, when)
    assert exc.value.reason_code == "missing_session_cost_evidence"

    # SessionState continuity itself is optional; constructing it with valid
    # factory outputs must succeed.
    session = SessionState(
        session_id="prior-1",
        current_route=current,
        last_served_route=current,
    )
    assert session.current_route.route_id == "omniroute/gc/current"


def test_build_production_bundle_injects_session_factories(tmp_path, monkeypatch):
    """Supervisor production factory must pass cost/task factories into CS."""
    from types import SimpleNamespace
    from typing import Any

    m = module()
    captured: dict[str, Any] = {}

    def fake_cs_bundle(**kwargs):
        captured.update(kwargs)
        assert kwargs.get("cost_state_factory") is not None
        assert kwargs.get("task_state_factory") is not None
        hooks = SimpleNamespace(
            seed_offers=kwargs.get("seed_offers"),
            cost_state_factory=kwargs["cost_state_factory"],
            task_state_factory=kwargs["task_state_factory"],
            prepare_execution_request=kwargs.get("prepare_execution_request"),
        )
        return SimpleNamespace(hooks=hooks, artifacts=SimpleNamespace(last_compiled=None))

    monkeypatch.setattr(m.CS, "build_production_controller_selection_bundle", fake_cs_bundle)
    # Avoid IntelligenceService construction.
    offer = _evidence_backed_offer(
        route_id="omniroute/gc/selected",
        provider="omniroute",
        model="gc/selected",
        cash_usd="0.05",
    )

    def seed_offers(mission, when):
        return (offer,)

    def prepare(task, criticality, context, request):
        return request

    def bind(route):
        return m.PrimeLaunchTarget(
            upstream_provider=route.provider,
            upstream_model=route.model,
            prime_provider=route.provider,
            prime_model=route.model,
            binding_digest=f"bind:{route.route_id}",
        )

    bundle = m.build_production_controller_selection_bundle(
        repo=tmp_path,
        state_dir=tmp_path / "state",
        prepare_execution_request=prepare,
        seed_offers=seed_offers,
        bind_prime_target=bind,
        require_live_sources=False,
    )
    assert captured["cost_state_factory"] is not None
    assert captured["task_state_factory"] is not None
    # Factories are real and evidence-backed.
    mission = m.ControllerMission(
        mission_id="m",
        story_id="s",
        attempt_id="a",
        objective="wire factories",
        context_burden="medium",
    )
    when = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)
    # seed first so cost cache fills
    seed = captured["seed_offers"] or seed_offers
    seed(mission, when)
    task = captured["task_state_factory"](mission)
    assert task.required_capability_tier == 2
    cost = captured["cost_state_factory"](offer.route, offer.route, when)
    assert str(cost.stay_expected.cash_usd) == "0.05"
    assert bundle.hooks.cost_state_factory is not None


def test_controller_selection_uses_canonical_module_identity():
    """Path loading must not create a duplicate selector module/classes."""
    m = module()
    assert m.CS is sys.modules["verdict.controller_selection"]
    assert m.CS.__name__ == "verdict.controller_selection"
    assert m.CS.ControllerLaunchError is m.CL.ControllerLaunchError
    assert "verdict_controller_selection" not in sys.modules


def test_prior_session_state_is_pessimistic_until_live_preparation(tmp_path):
    """Persisted history must never claim a currently eligible route."""
    m = module()
    state = tmp_path / "state"
    state.mkdir()
    _write_controller_decision(
        state / "controller-decision-previous.json",
        provider="omniroute",
        model="gc/previous",
    )

    prior = m.load_session_state_from_prior(state_dir=state, repo=tmp_path)

    assert prior is not None
    assert prior.current_route.eligible is False
    assert prior.current_route.hard_ineligible is True
    assert prior.current_route.exclusion_reason == "prior_route_unverified"
    # No prior artifact means NEW continuity, never a fabricated STAY state.
    empty = tmp_path / "empty-state"
    empty.mkdir()
    assert m.load_session_state_from_prior(state_dir=empty, repo=tmp_path) is None


@pytest.mark.parametrize("inject_fn", [False, True])
def test_supervisor_production_bundle_certifies_loaded_passports(tmp_path, inject_fn):
    """Supervisor forwards the report producer; defaults use real offline BOD-92."""
    from dataclasses import replace

    from verdict.model_passports import ModelPassport
    from verdict.runtime_certification import CertificationState, certify_runtime

    m = module()
    when = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    identity = "omniroute/gc/selected"
    passport = ModelPassport(
        provider="omniroute",
        model_id=identity,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=when,
        last_verified_timestamp=when,
        expires_at=when + timedelta(minutes=30),
        tool_support=True,
        token_cost_per_1k=0.001,
    )
    calls = []

    def certify(**kwargs):
        calls.append(kwargs)
        # Injected hook still returns genuine BOD-92 report contents.
        return certify_runtime(**kwargs)

    def force_unknown(**kwargs):
        calls.append(kwargs)
        report = certify_runtime(**kwargs)
        return replace(
            report,
            components=tuple(
                replace(component, state=CertificationState.UNKNOWN)
                for component in report.components
            ),
        )

    bundle = m.build_production_controller_selection_bundle(
        repo=tmp_path,
        state_dir=tmp_path / "state",
        prepare_execution_request=lambda task, criticality, context, request: request,
        load_healthy_passports_fn=lambda path: {identity: passport},
        load_metadata_fn=lambda path: object(),
        free_identity_ids=frozenset({identity}),
        certify_runtime_fn=force_unknown if inject_fn else None,
        bind_prime_target=lambda route: m.PrimeLaunchTarget(
            upstream_provider=route.provider,
            upstream_model=route.model,
            prime_provider=route.provider,
            prime_model=route.model,
            binding_digest="trusted-binding",
        ),
    )
    mission = m.ControllerMission(
        mission_id="m", story_id="s", attempt_id="a", objective="certify controller"
    )
    if inject_fn:
        seeds = bundle.hooks.seed_offers(mission, when)
        assert len(seeds) == 1
        assert seeds[0].certification_state is CertificationState.UNKNOWN
        assert seeds[0].certification_freshness == "fresh"
        assert seeds[0].route.eligible is False
        assert len(calls) == 1
        assert calls[0]["run_registered_detectors"] is False
        assert calls[0]["snapshots"][0].component_id == identity
        with pytest.raises(m.ControllerLaunchError) as blocked:
            m.select_controller_launch(mission, hooks=bundle.hooks, now=when)
        assert blocked.value.reason_code == "no_eligible_route"
    else:
        seeds = bundle.hooks.seed_offers(mission, when)
        assert len(seeds) == 1
        assert seeds[0].route.route_id == identity
        assert seeds[0].certification_state is CertificationState.READY
        assert seeds[0].certification_freshness == "fresh"
        assert seeds[0].route.eligible is False
        assert not calls  # default real certify_runtime path, no injected hook
