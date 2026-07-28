"""Unit tests for verdict.daemon."""

from pathlib import Path

from verdict.daemon import VerdictProactiveDaemon


def test_proactive_daemon_scan_and_remediate(tmp_path: Path) -> None:
    daemon = VerdictProactiveDaemon(cwd=tmp_path, home_dir=tmp_path)
    results = daemon.run_health_scan_and_remediate()

    assert len(results) >= 2
    check_names = {r.check_name for r in results}
    assert "memory_plane_db" in check_names
    assert "tool_bridge_mcp" in check_names
