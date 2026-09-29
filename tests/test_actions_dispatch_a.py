"""Golden tests for BOD-275 dispatch-a: verify CLI output matches origin/main.

Golden tests compare full stdout + exit-code against fixtures captured from
origin/main's CLI through the identical ``_run_verdict`` harness.
Structural tests cover commands whose text output includes absolute paths
(Rich truncation depends on process-internal Console width) or live state.
"""

from __future__ import annotations

import json
import os
import re
import sys
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "actions_dispatch_a"


def _load_fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / f"{name}.json").read_text())


def _normalise(text: str, home: str) -> str:
    """Normalise non-deterministic tokens for golden comparison."""
    text = text.replace(home, "<HOME>")
    text = text.replace(str(Path.cwd()), "<CWD>")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"sha256:[0-9a-f]{64}", "sha256:<DIGEST>", text)
    text = re.sub(r"[0-9a-f]{40}", "<SHA40>", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?", "<TS>", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _run_verdict(argv: list[str], home: str) -> tuple[str, str, int]:
    """Invoke verdict.cli.main() with *argv* in a hermetic env."""
    old_argv = sys.argv[:]
    snap: dict[str, str | None] = {}
    env_set = {
        "HOME": home,
        "VERDICT_MEMORY_DB": str(Path(home) / ".verdict" / "memory.db"),
        "NO_COLOR": "1",
        "TERM": "dumb",
        "COLUMNS": "300",
        "XDG_CONFIG_HOME": str(Path(home) / ".config"),
        "XDG_DATA_HOME": str(Path(home) / ".local" / "share"),
    }
    for k in (
        "OMNIROUTE_BASE_URL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "VERDICT_CONFIG",
        "VERDICT_YAML",
    ):
        snap[k] = os.environ.pop(k, None)
    for k, v in env_set.items():
        snap[k] = os.environ.get(k)
        os.environ[k] = v
    sys.argv = ["verdict", *argv]
    out_buf, err_buf = StringIO(), StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    exit_code = 0
    try:
        from verdict.cli import main

        main()
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
    except Exception:
        exit_code = 1
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        sys.argv = old_argv
        for k, v in snap.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return out_buf.getvalue(), err_buf.getvalue(), exit_code


def _assert_golden(
    fixture_name: str, argv: list[str], home: str, *, expected_exit: int | None = None
) -> str:
    fix = _load_fixture(fixture_name)
    stdout, _stderr, ec = _run_verdict(argv, home)
    want = expected_exit if expected_exit is not None else fix["exit_code"]
    assert ec == want, f"{fixture_name}: exit {ec} != {want}"
    actual = _normalise(stdout, home)
    expected = _normalise(fix["stdout"], "<HOME>")
    assert actual == expected, (
        f"{fixture_name} mismatch\n--- expect ---\n{expected[:400]}\n--- actual ---\n{actual[:400]}"
    )
    return stdout


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "h"
    home.mkdir()
    (home / ".verdict").mkdir()
    (home / ".config" / "verdict").mkdir(parents=True)
    (home / ".local" / "share").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VERDICT_MEMORY_DB", str(home / ".verdict" / "memory.db"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")

    # Create sentinel files so hook.status / mcp.status see them (hermetic cwd).
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "CLAUDE.md").write_text("# Verdict Unified Memory Bridge\n")
    (cwd / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"verdict-memory": {"command": "echo"}}})
    )
    monkeypatch.chdir(cwd)


def _home(tmp_path: Path) -> str:
    return str(tmp_path / "h")


class TestMemoryPut:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("memory_put", ["memory", "put", "test_key", "test_content"], _home(tmp_path))


class TestMemorySearch:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("memory_search", ["memory", "search", "test_query"], _home(tmp_path))


class TestHookStatus:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("hook_status", ["hook", "status"], _home(tmp_path))


class TestHookStatusJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("hook_status_json", ["hook", "status", "--json"], _home(tmp_path))


class TestHookRecall:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("hook_recall", ["hook", "recall", "test_query"], _home(tmp_path))


class TestHookRecallJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden(
            "hook_recall_json", ["hook", "recall", "test_query", "--json"], _home(tmp_path)
        )


class TestHookRecord:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("hook_record", ["hook", "record", "testk", "testv"], _home(tmp_path))


class TestHookConfigureJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("hook_configure_json", ["hook", "configure", "--json"], _home(tmp_path))


class TestMcpStatus:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("mcp_status", ["mcp", "status"], _home(tmp_path))


class TestMcpStatusJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("mcp_status_json", ["mcp", "status", "--json"], _home(tmp_path))


class TestMcpInitJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("mcp_init_json", ["mcp", "init", "--json"], _home(tmp_path))


class TestRuntimeStatusJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden("runtime_status_json", ["runtime", "status", "--json"], _home(tmp_path))


class TestRuntimeReconcileJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden(
            "runtime_reconcile_json", ["runtime", "reconcile", "--json"], _home(tmp_path)
        )


class TestProveAtRestOnceNoConsent:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden(
            "prove_at_rest_once_no_consent",
            ["prove-at-rest", "once"],
            _home(tmp_path),
            expected_exit=2,
        )


class TestProveAtRestOnceNoConsentJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden(
            "prove_at_rest_once_no_consent_json",
            ["prove-at-rest", "once", "--json"],
            _home(tmp_path),
            expected_exit=2,
        )


class TestProveAtRestStatusJson:
    def test_golden(self, tmp_path: Path) -> None:
        _assert_golden(
            "prove_at_rest_status_json", ["prove-at-rest", "status", "--json"], _home(tmp_path)
        )


class TestHookConfigure:
    """Substring: configure output varies with installed tools and bridge state."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["hook", "configure"], _home(tmp_path))
        assert ec == 0
        assert "Hook / configure" in stdout or "Memory bridge" in stdout


class TestMcpInit:
    """Substring: init output varies with existing .mcp.json state."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["mcp", "init"], _home(tmp_path))
        assert ec == 0
        assert "MCP / init" in stdout
        assert "initialized" in stdout


class TestMemorySetup:
    """Substring: setup creates/opens DB; output varies with pre-existing state."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["memory", "setup"], _home(tmp_path))
        assert ec == 0
        assert "Memory / setup" in stdout or "Memory database ready" in stdout


class TestRuntimeStatus:
    """Substring: runtime status includes live process state and paths."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["runtime", "status"], _home(tmp_path))
        assert ec == 0
        assert "Runtime" in stdout


class TestRuntimeReconcile:
    """Substring: reconcile output varies with live runtime state."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["runtime", "reconcile"], _home(tmp_path))
        assert ec == 0
        assert "Runtime" in stdout


class TestProveAtRestStatus:
    """Substring: prove-at-rest status varies with consent and schedule state."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["prove-at-rest", "status"], _home(tmp_path))
        assert ec == 0
        assert "prove-at-rest" in stdout.lower() or "Prove at rest" in stdout


class TestMemoryExport:
    """Substring: export path in output is test-specific (tmp_path)."""

    def test_structure(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        stdout, _, ec = _run_verdict(
            ["memory", "export", "--output", str(Path(home) / "e.json")], home
        )
        assert ec == 0
        assert "Exported memory manifest" in stdout


class TestMemorySearchAfterPut:
    def test_golden(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        _run_verdict(["memory", "put", "test_key", "test_content"], home)
        _assert_golden("memory_search_after_put", ["memory", "search", "test_key"], home)


class TestMemoryImport:
    def test_golden(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        _run_verdict(["memory", "put", "test_key", "test_content"], home)
        ep = str(Path(home) / "e.json")
        _run_verdict(["memory", "export", "--output", ep], home)
        _assert_golden("memory_import", ["memory", "import", ep], home)


class TestMemoryDocs:
    """Substring: memory docs listing varies with installed documentation."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["memory", "docs"], _home(tmp_path))
        assert ec == 1
        assert "Memory / docs" in stdout or "documentation" in stdout.lower()

    def test_json_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["memory", "docs", "--json"], _home(tmp_path))
        assert ec == 1
        data = json.loads(stdout)
        assert "errors" in data or "operation" in data


class TestRuntimeExplain:
    """Substring: explain output includes live runtime configuration and paths."""

    def test_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["runtime", "explain"], _home(tmp_path))
        assert ec == 0
        assert "Runtime" in stdout

    def test_json_structure(self, tmp_path: Path) -> None:
        stdout, _, ec = _run_verdict(["runtime", "explain", "--json"], _home(tmp_path))
        assert ec == 0
        data = json.loads(stdout)
        assert isinstance(data, dict)


@pytest.fixture()
def packet_file(tmp_path: Path) -> Path:
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
    from verdict.execution_packet import ExecutionPacket, ExecutionPacketStore

    pkt = ExecutionPacket.from_dict(payload)
    pkt_path = tmp_path / "h" / "test_packet.json"
    ExecutionPacketStore(tmp_path / "h").create(pkt, pkt_path)
    return pkt_path


@pytest.fixture()
def packet_source(tmp_path: Path) -> Path:
    digest_a = "sha256:" + "a" * 64
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
            {"task_id": "T1", "description": "Test task.", "status": "pending", "dependencies": []}
        ],
        "route_attempts": [],
        "failure_history": [],
        "transitions": [],
        "checkpoint_refs": [],
        "receipt_refs": [],
        "next_safe_action": "inspect",
        "proof_level": "unknown",
    }
    source = tmp_path / "h" / "create_source.json"
    source.write_text(json.dumps(payload))
    return source


class TestAutodevPacketInspect:
    def test_golden(self, tmp_path: Path, packet_file: Path) -> None:
        _assert_golden(
            "autodev_packet_inspect",
            ["autodev", "packet", "inspect", "--packet", str(packet_file)],
            _home(tmp_path),
        )


class TestAutodevPacketInspectJson:
    def test_golden(self, tmp_path: Path, packet_file: Path) -> None:
        _assert_golden(
            "autodev_packet_inspect_json",
            ["autodev", "packet", "inspect", "--packet", str(packet_file), "--json"],
            _home(tmp_path),
        )


class TestAutodevPacketValidate:
    def test_golden(self, tmp_path: Path, packet_file: Path) -> None:
        _assert_golden(
            "autodev_packet_validate",
            ["autodev", "packet", "validate", "--packet", str(packet_file)],
            _home(tmp_path),
        )


class TestAutodevPacketValidateJson:
    def test_golden(self, tmp_path: Path, packet_file: Path) -> None:
        _assert_golden(
            "autodev_packet_validate_json",
            ["autodev", "packet", "validate", "--packet", str(packet_file), "--json"],
            _home(tmp_path),
        )


class TestAutodevPacketResume:
    def test_golden(self, tmp_path: Path, packet_file: Path) -> None:
        _assert_golden(
            "autodev_packet_resume",
            ["autodev", "packet", "resume", "--packet", str(packet_file), "--model", "test-model"],
            _home(tmp_path),
        )


class TestAutodevPacketResumeJson:
    def test_golden(self, tmp_path: Path, packet_file: Path) -> None:
        _assert_golden(
            "autodev_packet_resume_json",
            [
                "autodev",
                "packet",
                "resume",
                "--packet",
                str(packet_file),
                "--model",
                "test-model",
                "--json",
            ],
            _home(tmp_path),
        )


class TestAutodevPacketCreateJson:
    def test_golden(self, tmp_path: Path, packet_source: Path) -> None:
        home = _home(tmp_path)
        target = Path(home) / "created.json"
        _assert_golden(
            "autodev_packet_create_json",
            [
                "autodev",
                "packet",
                "create",
                "--packet",
                str(target),
                "--from",
                str(packet_source),
                "--json",
            ],
            home,
        )
        assert target.exists()


class TestAutodevPacketCompare:
    def test_compare_error(self, tmp_path: Path, packet_file: Path) -> None:
        home = _home(tmp_path)
        fa, fb = Path(home) / "fa.json", Path(home) / "fb.json"
        fa.write_text(json.dumps({"run_id": "a", "results": []}))
        fb.write_text(json.dumps({"run_id": "b", "results": []}))
        stdout, _, ec = _run_verdict(
            [
                "autodev",
                "packet",
                "compare",
                "--packet",
                str(packet_file),
                "--a",
                str(fa),
                "--b",
                str(fb),
                "--json",
            ],
            home,
        )
        assert ec != 0
        assert "error" in json.loads(stdout)


class TestAutodevPacketNotFound:
    def test_inspect_missing(self, tmp_path: Path) -> None:
        home = _home(tmp_path)
        stdout, _, ec = _run_verdict(
            ["autodev", "packet", "inspect", "--packet", str(Path(home) / "nope.json"), "--json"],
            home,
        )
        assert ec != 0
        assert "error" in json.loads(stdout)
