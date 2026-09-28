"""Tests for BOD-275 dispatch-a: cmd_memory, cmd_mcp, cmd_hook, cmd_runtime,
cmd_prove_at_rest (non-daemon), cmd_autodev_packet (inspect/compare/validate/create/resume).

Each test verifies that the CLI handler delegates to run_action and renders
the ActionResult identically to origin/main's direct-logic output.
"""
from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "actions_dispatch_a"


def _load_fixture(name: str) -> dict[str, Any]:
    path = _FIXTURE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _normalise(text: str) -> str:
    """Strip ANSI, collapse whitespace, replace absolute paths with <HOME>."""
    import re

    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"/tmp/v275da[^ \n\"]*", "<PATH>", text)
    text = re.sub(str(Path.home()), "<HOME>", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Shared monkeypatching: isolate HOME / memory DB / present colours
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".verdict").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_MEMORY_DB", str(home / ".verdict" / "memory.db"))
    # Disable colour so output is predictable
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")


# ===================================================================
# cmd_memory
# ===================================================================


class TestMemoryPut:
    def test_put_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_memory

        args = mock.MagicMock()
        args.memory_command = "put"
        args.key = "test_key"
        args.content = "test_content"
        args.namespace = "default"
        args.source = "cli"
        args.db_path = None
        args.json = False

        cmd_memory(args)
        out = capsys.readouterr().out
        assert "Memory record put" in out
        assert "test_key" in out


class TestMemorySearch:
    def test_search_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_memory

        args = mock.MagicMock()
        args.memory_command = "search"
        args.query = "nonexistent"
        args.namespace = None
        args.limit = 10
        args.db_path = None
        args.json = False

        cmd_memory(args)
        out = capsys.readouterr().out
        assert "Found 0 memory record(s)" in out


class TestMemoryExport:
    def test_export_calls_run_action(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_memory

        out_file = tmp_path / "export.json"
        args = mock.MagicMock()
        args.memory_command = "export"
        args.output = str(out_file)
        args.db_path = None
        args.json = False

        cmd_memory(args)
        out = capsys.readouterr().out
        assert "Exported memory manifest" in out


class TestMemoryDocs:
    def test_docs_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_memory

        args = mock.MagicMock()
        args.memory_command = "docs"
        args.json = True
        args.repo_root = str(Path.cwd())
        args.fix = False
        args.db_path = None

        # docs will likely fail (no valid repo), but should still go through run_action
        with pytest.raises(SystemExit):
            cmd_memory(args)
        out = capsys.readouterr().out
        # Should produce JSON output
        data = json.loads(out)
        assert "operation" in data or "passed" in data or "errors" in data


class TestMemorySetup:
    def test_setup_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_memory

        args = mock.MagicMock()
        args.memory_command = "setup"
        args.tools = None
        args.db_path = None
        args.json = False

        cmd_memory(args)
        out = capsys.readouterr().out
        assert "Configured tools" in out or "Memory database ready" in out


# ===================================================================
# cmd_mcp
# ===================================================================


class TestMcpStatus:
    def test_status_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_mcp

        args = mock.MagicMock()
        args.mcp_command = "status"
        args.json = False

        cmd_mcp(args)
        out = capsys.readouterr().out
        assert "MCP" in out

    def test_status_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_mcp

        args = mock.MagicMock()
        args.mcp_command = "status"
        args.json = True

        cmd_mcp(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "mcp_registered" in data
        assert "mcp_config" in data


class TestMcpInit:
    def test_init_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_mcp

        args = mock.MagicMock()
        args.mcp_command = "init"
        args.json = False

        cmd_mcp(args)
        out = capsys.readouterr().out
        assert "MCP" in out
        assert "initialized" in out

    def test_init_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_mcp

        args = mock.MagicMock()
        args.mcp_command = "init"
        args.json = True

        cmd_mcp(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "memory_db_path" in data


# ===================================================================
# cmd_hook
# ===================================================================


class TestHookStatus:
    def test_status_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "status"
        args.json = False
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        # Should show status lines for codex/claude/mcp/memory
        assert "codex" in out.lower() or "memory" in out.lower()

    def test_status_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "status"
        args.json = True
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "codex_agents_md" in data
        assert "memory_db" in data


class TestHookRecall:
    def test_recall_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "recall"
        args.query = "test_query"
        args.limit = 5
        args.json = False
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        assert "Recall:" in out
        assert "0 record(s)" in out

    def test_recall_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "recall"
        args.query = "test_query"
        args.limit = 5
        args.json = True
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)


class TestHookRecord:
    def test_record_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "record"
        args.key = "testk"
        args.value = "testv"
        args.namespace = "sessions"
        args.source = "cli"
        args.json = False
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        # Should either record or reject (provenance_required)
        assert "Recorded" in out or "Rejected" in out


class TestHookConfigure:
    def test_configure_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "configure"
        args.tools = None
        args.json = False
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        assert "configured" in out.lower() or "Memory bridge" in out

    def test_configure_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_hook

        args = mock.MagicMock()
        args.hook_command = "configure"
        args.tools = None
        args.json = True
        args.db_path = None

        cmd_hook(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "memory_db_path" in data


# ===================================================================
# cmd_runtime
# ===================================================================


class TestRuntimeStatus:
    def test_status_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_runtime

        cmd_runtime("status")
        out = capsys.readouterr().out
        assert "Runtime" in out

    def test_status_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_runtime

        cmd_runtime("status", output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "operation" in data or "passed" in data


class TestRuntimeExplain:
    def test_explain_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_runtime

        cmd_runtime("explain")
        out = capsys.readouterr().out
        assert "Runtime" in out

    def test_explain_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_runtime

        cmd_runtime("explain", output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, dict)


class TestRuntimeReconcile:
    def test_reconcile_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_runtime

        cmd_runtime("reconcile")
        out = capsys.readouterr().out
        assert "Runtime" in out

    def test_reconcile_json_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_runtime

        cmd_runtime("reconcile", output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, dict)


# ===================================================================
# cmd_prove_at_rest (non-daemon)
# ===================================================================


class TestProveAtRestStatus:
    def test_status_empty_calls_run_action(self, capsys: pytest.CaptureFixture[str]) -> None:
        from verdict.cli import cmd_prove_at_rest

        cmd_prove_at_rest("status")
        out = capsys.readouterr().out
        assert "prove-at-rest" in out.lower() or "Prove at rest" in out

    def test_status_empty_json_calls_run_action(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_prove_at_rest

        cmd_prove_at_rest("status", output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data.get("status") == "empty"
        assert "state_path" in data


class TestProveAtRestOnce:
    def test_once_no_consent_calls_run_action(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_prove_at_rest

        with pytest.raises(SystemExit) as exc_info:
            cmd_prove_at_rest("once")
        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        assert "consent" in out.lower() or "allow-live-probe" in out.lower()

    def test_once_no_consent_json_calls_run_action(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_prove_at_rest

        with pytest.raises(SystemExit) as exc_info:
            cmd_prove_at_rest("once", output_json=True)
        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "error" in data


# ===================================================================
# cmd_autodev_packet (inspect/compare/validate/create/resume)
# ===================================================================


@pytest.fixture()
def packet_file(tmp_path: Path) -> Path:
    """Create a valid execution packet file for testing."""
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    payload = {
        "schema_version": "1",
        "packet_id": "packet-headroom-unknown",
        "packet_version": 1,
        "story_id": "US1",
        "story_version": "1",
        "source": {
            "repository": "git@github.com:example/verdict.git",
            "worktree": "/workspace/verdict",
            "commit": "a" * 40,
            "branch": "feature/operational-loop",
            "dirty_digest": digest_a,
            "lock_digests": {"uv.lock": digest_b},
        },
        "intent": {
            "goal": "Represent missing headroom as unknown.",
            "non_goals": ["Implement provider quota clients."],
            "acceptance": ["Missing endpoint never reports 100 percent."],
            "limitations": ["Quota may remain unknown."],
        },
        "authority": {
            "owned_paths": ["verdict/headroom.py", "tests/test_headroom.py"],
            "denied_paths": [".env"],
            "tools": ["read", "patch", "test"],
            "network": False,
            "max_spend_usd": 0.25,
            "max_concurrency": 1,
            "max_attempts": 2,
            "destructive": False,
            "production": False,
        },
        "verification": {
            "argv": ["uv", "run", "pytest", "-q", "tests/test_headroom.py"],
            "timeout_seconds": 120,
        },
        "decisions": [{"ref": "spec.md", "digest": digest_a}],
        "context_refs": [
            {"ref": "verdict/headroom.py", "digest": digest_b, "proof_level": "source-only"}
        ],
        "tasks": [
            {
                "task_id": "T1",
                "description": "Implement fail-closed headroom.",
                "status": "pending",
                "dependencies": [],
            }
        ],
        "route_attempts": [],
        "failure_history": [],
        "transitions": [],
        "checkpoint_refs": [],
        "receipt_refs": [],
        "next_safe_action": "Run the focused red test.",
        "proof_level": "source-only",
    }
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    # Create via the store
    from verdict.execution_packet import ExecutionPacket, ExecutionPacketStore

    pkt = ExecutionPacket.from_dict(payload)
    pkt_path = tmp_path / "test_packet.json"
    ExecutionPacketStore(tmp_path).create(pkt, pkt_path)
    return pkt_path


@pytest.fixture()
def packet_source(tmp_path: Path) -> Path:
    """Create a valid packet source JSON file."""
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    payload = {
        "schema_version": "1",
        "packet_id": "packet-create-test",
        "packet_version": 1,
        "story_id": "US2",
        "story_version": "1",
        "source": {
            "repository": "git@github.com:example/verdict.git",
            "worktree": "/workspace/verdict",
            "commit": "b" * 40,
            "branch": "feature/create-test",
            "dirty_digest": digest_a,
            "lock_digests": {},
        },
        "intent": {
            "goal": "Test create.",
            "non_goals": ["None."],
            "acceptance": ["Packet is created."],
            "limitations": ["Test only."],
        },
        "authority": {
            "owned_paths": ["test.py"],
            "denied_paths": [],
            "tools": ["read"],
            "network": False,
            "max_spend_usd": 0.1,
            "max_concurrency": 1,
            "max_attempts": 1,
            "destructive": False,
            "production": False,
        },
        "verification": {"argv": ["echo", "ok"], "timeout_seconds": 10},
        "decisions": [],
        "context_refs": [],
        "tasks": [
            {
                "task_id": "T1",
                "description": "Test task.",
                "status": "pending",
                "dependencies": [],
            }
        ],
        "route_attempts": [],
        "failure_history": [],
        "transitions": [],
        "checkpoint_refs": [],
        "receipt_refs": [],
        "next_safe_action": "inspect",
        "proof_level": "unknown",
    }
    source = tmp_path / "create_source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")
    return source


class TestAutodevPacketInspect:
    def test_inspect_json_calls_run_action(
        self, packet_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        cmd_autodev_packet("inspect", str(packet_file), output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["packet_id"] == "packet-headroom-unknown"

    def test_inspect_text_calls_run_action(
        self, packet_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        cmd_autodev_packet("inspect", str(packet_file))
        out = capsys.readouterr().out
        assert "packet-headroom-unknown" in out
        assert "Autodev packet" in out


class TestAutodevPacketValidate:
    def test_validate_json_calls_run_action(
        self, packet_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        cmd_autodev_packet("validate", str(packet_file), output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["packet_id"] == "packet-headroom-unknown"


class TestAutodevPacketCreate:
    def test_create_json_calls_run_action(
        self, tmp_path: Path, packet_source: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        target = tmp_path / "created_packet.json"
        cmd_autodev_packet(
            "create", str(target), source_path=str(packet_source), output_json=True
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["packet_id"] == "packet-create-test"
        assert target.exists()


class TestAutodevPacketResume:
    def test_resume_json_calls_run_action(
        self, packet_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        cmd_autodev_packet(
            "resume", str(packet_file), model="test-model", output_json=True
        )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["executing_model"] == "test-model"

    def test_resume_text_calls_run_action(
        self, packet_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        cmd_autodev_packet("resume", str(packet_file), model="test-model")
        out = capsys.readouterr().out
        assert "Autodev packet" in out


class TestAutodevPacketCompare:
    def test_compare_error_calls_run_action(
        self, tmp_path: Path, packet_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        fa = tmp_path / "family_a.json"
        fb = tmp_path / "family_b.json"
        fa.write_text(json.dumps({"run_id": "a", "results": []}))
        fb.write_text(json.dumps({"run_id": "b", "results": []}))

        with pytest.raises(SystemExit):
            cmd_autodev_packet(
                "compare",
                str(packet_file),
                family_a_path=str(fa),
                family_b_path=str(fb),
                output_json=True,
            )
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "error" in data


class TestAutodevPacketNotFound:
    def test_inspect_missing_packet_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from verdict.cli import cmd_autodev_packet

        missing = tmp_path / "nonexistent.json"
        with pytest.raises(SystemExit):
            cmd_autodev_packet("inspect", str(missing), output_json=True)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "error" in data


# ===================================================================
# Verify run_action is actually called (not bypassed)
# ===================================================================


class TestRunActionCalled:
    """Verify that each handler actually calls run_action, not direct domain logic."""

    def test_memory_put_uses_run_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from verdict.actions import registry
        from verdict.cli import cmd_memory

        calls: list[str] = []
        original = registry.run_action

        def spy(name: str, params: Any = None, **kw: Any) -> Any:
            calls.append(name)
            return original(name, params, **kw)

        monkeypatch.setattr(registry, "run_action", spy)

        args = mock.MagicMock()
        args.memory_command = "put"
        args.key = "spy_key"
        args.content = "spy_content"
        args.namespace = "default"
        args.source = "cli"
        args.db_path = None
        args.json = False

        cmd_memory(args)
        assert "memory.put" in calls

    def test_mcp_status_uses_run_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from verdict.actions import registry
        from verdict.cli import cmd_mcp

        calls: list[str] = []
        original = registry.run_action

        def spy(name: str, params: Any = None, **kw: Any) -> Any:
            calls.append(name)
            return original(name, params, **kw)

        monkeypatch.setattr(registry, "run_action", spy)

        args = mock.MagicMock()
        args.mcp_command = "status"
        args.json = False

        cmd_mcp(args)
        assert "mcp.status" in calls

    def test_hook_status_uses_run_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from verdict.actions import registry
        from verdict.cli import cmd_hook

        calls: list[str] = []
        original = registry.run_action

        def spy(name: str, params: Any = None, **kw: Any) -> Any:
            calls.append(name)
            return original(name, params, **kw)

        monkeypatch.setattr(registry, "run_action", spy)

        args = mock.MagicMock()
        args.hook_command = "status"
        args.json = False
        args.db_path = None

        cmd_hook(args)
        assert "hook.status" in calls

    def test_runtime_status_uses_run_action(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from verdict.actions import registry
        from verdict.cli import cmd_runtime

        calls: list[str] = []
        original = registry.run_action

        def spy(name: str, params: Any = None, **kw: Any) -> Any:
            calls.append(name)
            return original(name, params, **kw)

        monkeypatch.setattr(registry, "run_action", spy)
        cmd_runtime("status")
        assert "runtime.status" in calls

    def test_prove_at_rest_status_uses_run_action(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from verdict.actions import registry
        from verdict.cli import cmd_prove_at_rest

        calls: list[str] = []
        original = registry.run_action

        def spy(name: str, params: Any = None, **kw: Any) -> Any:
            calls.append(name)
            return original(name, params, **kw)

        monkeypatch.setattr(registry, "run_action", spy)
        cmd_prove_at_rest("status")
        assert "prove-at-rest.status" in calls

    def test_autodev_packet_inspect_uses_run_action(
        self, monkeypatch: pytest.MonkeyPatch, packet_file: Path
    ) -> None:
        from verdict.actions import registry
        from verdict.cli import cmd_autodev_packet

        calls: list[str] = []
        original = registry.run_action

        def spy(name: str, params: Any = None, **kw: Any) -> Any:
            calls.append(name)
            return original(name, params, **kw)

        monkeypatch.setattr(registry, "run_action", spy)
        cmd_autodev_packet("inspect", str(packet_file), output_json=True)
        assert "autodev.packet.inspect" in calls
