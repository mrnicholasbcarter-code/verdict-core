"""The presentation seam reports real work and never bypasses authorization."""

from pathlib import Path

from verdict.capability_bootstrap import run_bootstrap


def test_plan_is_observable_before_apply_and_installer_failure_is_reported(tmp_path: Path):
    events = []

    def observe(stage, state, summary, data):
        events.append((stage, state, summary))

    def install(action):
        assert any(stage == "plan" for stage, _, _ in events)
        assert events[-1][0:2] == ("action", "running")
        return {"status": "failed", "error": "offline"}

    report = run_bootstrap(
        providers=[],
        mode="apply",
        state_dir=tmp_path,
        consent=True,
        install_runner=install,
        observer=observe,
    )
    assert report.apply["mutated"] is False
    assert events[0][:2] == ("discover", "running")
    assert any(stage == "action" and state == "failed" for stage, state, _ in events)
    assert events[-1][:2] == ("certify", "ok")


def test_rejected_plan_cannot_install(tmp_path: Path):
    def forbidden(action):
        raise AssertionError("Install must not run after a declined plan")

    report = run_bootstrap(
        providers=[],
        mode="apply",
        state_dir=tmp_path,
        confirm_plan=lambda plan: False,
        install_runner=forbidden,
    )
    assert report.apply["mutated"] is False
    assert any(stage.status == "blocked" for stage in report.stages)


def test_resume_does_not_emit_install_activity(tmp_path: Path):
    def install(action):
        return {"status": "success"}

    run_bootstrap(
        providers=[], mode="apply", state_dir=tmp_path, consent=True, install_runner=install
    )
    events = []
    report = run_bootstrap(
        providers=[],
        mode="apply",
        state_dir=tmp_path,
        consent=True,
        install_runner=install,
        observer=lambda stage, state, summary, data: events.append((stage, state)),
    )
    assert report.apply.get("idempotent") is True
    assert ("action", "running") not in events
