"""Tests for memory bridge tool detection, preselection, and autopilot configuration."""

from pathlib import Path

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

    # Create dummy tool directories
    (home_dir / ".codex").mkdir()
    (cwd_dir / "CLAUDE.md").write_text("# Claude docs", encoding="utf-8")

    report = detect_available_tools(home_dir=home_dir, cwd=cwd_dir)
    assert report.detected_tools["codex"]["installed"] is True
    assert report.detected_tools["claude"]["installed"] is True
    assert report.detected_tools["pi"]["installed"] is False

    assert "codex" in report.preselected_tools
    assert "claude" in report.preselected_tools
    assert "pi" not in report.preselected_tools


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
    assert "claude" in res["configured_tools"]
    assert "pi" in res["configured_tools"]
    assert "ruflo" in res["configured_tools"]

    # Verify Codex AGENTS.md got instruction
    agents_md = cwd_dir / ".codex" / "AGENTS.md"
    assert agents_md.exists()
    assert "Verdict Unified Memory Bridge" in agents_md.read_text("utf-8")

    # Verify CLAUDE.md got instruction
    claude_md = cwd_dir / "CLAUDE.md"
    assert claude_md.exists()
    assert "Verdict Unified Memory Bridge" in claude_md.read_text("utf-8")


def test_memory_hook_controller(tmp_path: Path) -> None:
    plane = MemoryPlane(":memory:")
    controller = MemoryHookController(plane=plane)

    # 1. Test on_task_start
    t_res = controller.on_task_start("task_123", "Build memory bridge")
    assert t_res["status"] == "success"

    # 2. Test on_prompt
    augmented = controller.on_prompt("What is the memory bridge?")
    assert "Verdict Unified Memory" in augmented
    assert "What is the memory bridge?" in augmented

    # 3. Test on_tool_call
    tc_res = controller.on_tool_call(
        "run_command", {"cmd": "ls", "api_key": "secret123"}, "output_ok"
    )
    assert tc_res["status"] == "success"

    # 4. Test on_session_end
    se_res = controller.on_session_end(
        "sess_abc",
        transcript=[
            {"role": "user", "content": "How do I use memory?"},
            {"role": "assistant", "content": "Use MemoryPlane put and search."},
        ],
        receipts=[{"receipt_type": "decision", "scope": "sess_abc", "payload": {"model": "sol"}}],
    )
    assert se_res["status"] == "success"
    assert se_res["transcript_records_stored"] == 2
    assert se_res["receipts_logged"] == 1

    # Search plane to verify session items stored
    results = plane.search("MemoryPlane")
    assert len(results) >= 1
