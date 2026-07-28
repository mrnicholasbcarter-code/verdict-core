"""Tests for memory bridge tool detection, preselection, autopilot configuration, and 13-hook matrix."""

from pathlib import Path

import pytest

from verdict.memory_bridge import (
    MemoryHookController,
    configure_memory_bridge,
    detect_available_tools,
)
from verdict.memory_plane import MemoryPlane


def test_detect_available_tools(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    cwd_dir = tmp_path / "repo"
    home_dir.mkdir()
    cwd_dir.mkdir()

    (home_dir / ".codex").mkdir()
    (cwd_dir / "CLAUDE.md").write_text("# Claude docs", encoding="utf-8")

    report = detect_available_tools(home_dir=home_dir, cwd=cwd_dir)
    assert report.detected_tools["codex"]["installed"] is True
    assert report.detected_tools["claude"]["installed"] is True
    assert report.detected_tools["pi"]["installed"] is False

    assert "codex" in report.preselected_tools
    assert "claude" in report.preselected_tools


def test_configure_memory_bridge(tmp_path: Path) -> None:
    home_dir = tmp_path / "home"
    cwd_dir = tmp_path / "repo"
    home_dir.mkdir()
    cwd_dir.mkdir()

    plane = MemoryPlane(cwd_dir / "memory.db")

    res = configure_memory_bridge(
        selected_tools=["codex", "claude", "pi", "ruflo"],
        plane=plane,
        home_dir=home_dir,
        cwd=cwd_dir,
    )

    assert res["status"] == "success"
    assert "codex" in res["configured_tools"]

    agents_md = cwd_dir / ".codex" / "AGENTS.md"
    assert agents_md.exists()
    assert "Verdict Unified Memory Bridge" in agents_md.read_text("utf-8")


def test_memory_hook_controller_all_13_hooks(tmp_path: Path) -> None:
    plane = MemoryPlane(":memory:")
    controller = MemoryHookController(plane=plane)

    # 1. Prompt & Response Hooks
    aug_prompt = controller.on_prompt("How do I store context?")
    assert "Verdict Unified Memory" in aug_prompt
    resp_res = controller.on_response("Response body", session_id="sess_1")
    assert resp_res["status"] == "success"

    # 2. Task Hooks
    ts_res = controller.on_task_start("task_1", "Build hook suite")
    assert ts_res["status"] == "success"
    tc_res = controller.on_task_complete("task_1", status="complete")
    assert tc_res["status"] == "success"

    # 3. File Edit Hooks
    fe_res = controller.on_file_edit_start("src/app.py")
    assert fe_res["status"] == "success"
    with pytest.raises(ValueError, match="quarantined_path_rejected"):
        controller.on_file_edit_start("/tmp/unsafe.py")
    fec_res = controller.on_file_edit_complete("src/app.py", diff_hash="hash123")
    assert fec_res["status"] == "success"

    # 4. Command Execution Hooks
    ce_res = controller.on_command_execute("ls -la")
    assert ce_res["status"] == "success"
    with pytest.raises(ValueError, match="destructive_command_rejected"):
        controller.on_command_execute("rm -rf /")
    cc_res = controller.on_command_complete("ls -la", exit_code=0, duration_ms=12.5)
    assert cc_res["status"] == "success"

    # 5. Session Hooks
    ss_res = controller.on_session_start("sess_1")
    assert ss_res["status"] == "success"
    se_res = controller.on_session_end("sess_1", transcript=[{"role": "user", "content": "Hello"}])
    assert se_res["status"] == "success"
    sr_res = controller.on_session_restore("sess_1")
    assert sr_res["status"] == "success"

    # 6. Verification & Error Hooks
    v_res = controller.on_verify("test_suite", status="passed")
    assert v_res["status"] == "success"
    e_res = controller.on_error("Network timeout")
    assert e_res["status"] == "success"
