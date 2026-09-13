import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


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
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/prime_supervisor.py"),
            "--repo",
            str(repo),
            "--prime",
            str(fake),
            "--provider",
            "test",
            "--model",
            "exact",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists(), "worker never launched"
        child_pid = int(pid_file.read_text())
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
            "--provider",
            "test",
            "--model",
            "exact",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
            "--max-restarts",
            "0",
        ],
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
            "--provider",
            "test",
            "--model",
            "exact",
            "--idle-seconds",
            "30",
            "--timeout",
            "60",
            "--max-restarts",
            "0",
        ],
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
            "--provider",
            "test",
            "--model",
            "exact",
            "--idle-seconds",
            "1",
            "--timeout",
            "5",
            "--poll-seconds",
            ".02",
            "--max-restarts",
            "1",
        ],
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
            "--provider",
            "test",
            "--model",
            "exact",
            "--idle-seconds",
            ".05",
            "--timeout",
            "5",
            "--poll-seconds",
            ".02",
            "--max-restarts",
            "0",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    checkpoint = json.loads((state / "supervisor.json").read_text())
    assert checkpoint["status"] == "BLOCKED"
    assert checkpoint["result"]["reason"] == "NO_PROGRESS"
    assert checkpoint["result"]["returncode"] is not None
