import subprocess

from verdict.golden_path import Stage, StageStatus, run_golden_path


def _repo(tmp_path):
    repo = tmp_path / "sample"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "check.py").write_text("print('ok')\n")
    subprocess.run(["git", "-C", str(repo), "add", "check.py"], check=True)
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
            "-qm",
            "init",
        ],
        check=True,
    )
    return repo


def test_real_repository_runs_ordered_durable_three_stage_path(tmp_path):
    repo = _repo(tmp_path)
    first = run_golden_path("verify sample", repo, memory_path=tmp_path / "memory.db", clock=1.0)
    second = run_golden_path("verify sample", repo, memory_path=tmp_path / "memory.db", clock=2.0)

    assert first.decision == "accepted"
    assert [item.stage for item in first.stages] == list(Stage)
    assert all(item.status == StageStatus.PASSED for item in first.stages)
    assert first.report_digest == second.report_digest
    assert all("/home/" not in str(item.to_dict()) for item in first.stages)


def test_dirty_repository_fails_closed_before_memory_or_verification(tmp_path):
    repo = _repo(tmp_path)
    (repo / "uncommitted.txt").write_text("not committed")
    report = run_golden_path("verify sample", repo, memory_path=tmp_path / "memory.db")

    assert report.decision == "denied"
    assert [item.stage for item in report.stages] == list(Stage)
    assert report.stages[0].status == StageStatus.FAILED


def test_failed_verification_is_evidence_and_denies(tmp_path):
    repo = _repo(tmp_path)
    report = run_golden_path(
        "verify sample",
        repo,
        memory_path=tmp_path / "memory.db",
        verification_command=("sh", "-c", "exit 7"),
    )

    assert report.decision == "denied"
    assert report.stages[-1].status == StageStatus.FAILED
    assert report.stages[-1].evidence["exit_code"] == 7


def test_timeout_is_bounded_and_denies(tmp_path):
    report = run_golden_path(
        "verify sample",
        _repo(tmp_path),
        memory_path=tmp_path / "memory.db",
        verification_command=("python", "-c", "import time; time.sleep(2)"),
        timeout_seconds=0.05,
    )
    assert report.decision == "denied"
    assert report.stages[-1].evidence["timed_out"] is True


def test_unavailable_memory_fails_closed(tmp_path):
    repo = _repo(tmp_path)
    memory_path = tmp_path / "memory-directory"
    memory_path.mkdir()
    report = run_golden_path("verify sample", repo, memory_path=memory_path)
    assert report.decision == "denied"
    assert report.stages[1].status == StageStatus.UNAVAILABLE
    assert report.stages[2].status == StageStatus.UNKNOWN


def test_changed_path_outside_declared_boundary_denies(tmp_path):
    report = run_golden_path(
        "verify sample",
        _repo(tmp_path),
        memory_path=tmp_path / "memory.db",
        verification_command=("python", "-c", "open('created.txt', 'w').write('x')"),
        owned_paths=("allowed/",),
    )
    assert report.decision == "denied"
    assert report.stages[-1].evidence["outside_owned_paths"] == 1
