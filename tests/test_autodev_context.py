"""Focused contract tests for the deterministic US1 worker context pack.

These tests intentionally target the thin autodev seam that T015 will add over
``ContextPackCompiler``.  They describe the worker-facing contract without
introducing a second context representation or retrieval framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import verdict.autodev_run as autodev_run
from verdict.execution_packet import ExecutionPacket, capture_source_binding
from verdict.receipt_store import ReceiptStore


def _compile_context(**overrides: Any) -> Any:
    """Call the prospective public autodev context seam with frozen inputs."""
    request: dict[str, Any] = {
        "objective": "Represent unavailable provider headroom as unknown.",
        "non_goals": (
            "Do not add provider-specific quota clients.",
            "Do not enable gateway-native routing.",
        ),
        "acceptance": (
            "An absent endpoint must not fabricate capacity.",
            "The focused headroom tests pass independently.",
        ),
        "authority": {
            "owned_paths": ("verdict/headroom.py", "tests/test_headroom.py"),
            "denied_paths": ("verdict/cli.py",),
            "tools": ("git", "pytest"),
            "network": False,
        },
        "owned_source": {
            "verdict/headroom.py": "def provider_headroom(): return unavailable",
            "tests/test_headroom.py": "def test_absent_endpoint_is_unknown(): ...",
        },
        "repository_instructions": (
            "Keep files under the repository source and tests directories.",
        ),
        "relevant_examples": (
            "tests/test_headroom.py::test_absent_endpoint_is_unknown",
        ),
        "governing_docs": (
            "specs/272-operational-routing-loop/spec.md",
            "ADR-010: missing hard evidence fails closed",
        ),
        "symbol_relationship": "headroom.py:provider_headroom -> test_headroom.py",
        "token_budget": 512,
    }
    request.update(overrides)

    # Keep collection healthy while making the missing prospective seam the
    # intentional red failure until T015 supplies it.
    assert hasattr(autodev_run, "compile_worker_context"), (
        "T015 must expose compile_worker_context over ContextPackCompiler"
    )
    return autodev_run.compile_worker_context(**request)


def test_worker_context_contains_contract_sections_and_provenance() -> None:
    """The worker receives intent, authority, bounded source, and evidence."""
    pack = _compile_context()

    prompt = pack.compiled_prompt
    for expected in (
        "Represent unavailable provider headroom as unknown.",
        "Do not add provider-specific quota clients.",
        "An absent endpoint must not fabricate capacity.",
        "verdict/headroom.py",
        "pytest",
        "def provider_headroom(): return unavailable",
        "tests/test_headroom.py::test_absent_endpoint_is_unknown",
        "specs/272-operational-routing-loop/spec.md",
        "headroom.py:provider_headroom -> test_headroom.py",
    ):
        assert expected in prompt

    assert pack.used_tokens <= pack.token_budget == 512
    assert pack.units
    assert all(unit.source_uri for unit in pack.units)
    assert all(unit.source_digest.startswith("sha256:") for unit in pack.units)
    assert all(decision.unit_id for decision in pack.decisions)
    assert pack.receipt.verify(pack)


def test_worker_context_is_byte_identical_across_two_runs_on_the_same_inputs() -> None:
    first = _compile_context()
    second = _compile_context()
    assert first.digest == second.digest
    assert first.canonical_json() == second.canonical_json()
    assert first.compiled_prompt == second.compiled_prompt


def test_worker_context_omits_optional_symbol_relationship_without_placeholder() -> None:
    """No symbol relationship is invented when deterministic discovery has none."""
    pack = _compile_context(symbol_relationship=None)

    assert "headroom.py:provider_headroom -> test_headroom.py" not in pack.compiled_prompt
    assert not any(unit.key == "symbol_relationship" for unit in pack.units)


def test_worker_context_records_omissions_when_token_budget_is_exhausted() -> None:
    """Budget pressure is explicit and bounded instead of silently overflowing."""
    pack = _compile_context(
        token_budget=48,
        owned_source={
            "verdict/headroom.py": "source line " * 400,
            "tests/test_headroom.py": "test line " * 400,
        },
        governing_docs=("governing document " * 400,),
    )

    assert pack.used_tokens <= 48
    assert pack.truncated_count > 0
    assert any(
        decision.action == "exclude"
        and decision.reason == "input_budget_exhausted"
        for decision in pack.decisions
    )


def _packet(repo: Path) -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="headroom-unknown",
        packet_version=1,
        story_id="US1",
        story_version="1",
        source=capture_source_binding(
            repo,
            repository="git@example.test:verdict.git",
            lock_paths=(),
        ),
        intent={
            "goal": "Represent unavailable provider headroom as unknown.",
            "non_goals": ["Do not add provider-specific quota clients."],
            "acceptance": ["An absent endpoint must not fabricate capacity."],
            "limitations": [],
        },
        authority={
            "owned_paths": ["verdict/headroom.py", "tests/test_headroom.py"],
            "denied_paths": ["verdict/cli.py"],
            "tools": ["read", "patch", "test"],
            "network": False,
            "max_spend_usd": 0.25,
            "max_concurrency": 1,
            "max_attempts": 2,
            "destructive": False,
            "production": False,
        },
        verification={
            "argv": ["uv", "run", "pytest", "-q", "tests/test_headroom.py"],
            "timeout_seconds": 120,
        },
        decisions=[],
        context_refs=[],
        tasks=[
            {
                "task_id": "headroom-unknown",
                "description": "Implement the bounded headroom change.",
                "status": "pending",
                "dependencies": [],
            }
        ],
        route_attempts=[],
        failure_history=[],
        transitions=[],
        checkpoint_refs=[],
        receipt_refs=[],
        next_safe_action="Compile the worker context.",
        proof_level="source-only",
    )


def test_packet_context_is_compiled_from_owned_repository_inputs_and_persisted(
    tmp_path: Path,
) -> None:
    """T015 wires the compiler to packet inputs rather than caller-written prose."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "verdict").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "specs").mkdir()
    (tmp_path / "verdict" / "headroom.py").write_text(
        "def check_headroom():\n    return True, 100.0\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_headroom.py").write_text(
        "def test_missing_is_unknown(): ...\n", encoding="utf-8"
    )
    (tmp_path / "AGENTS.md").write_text("Missing evidence remains unknown.\n", encoding="utf-8")
    (tmp_path / "specs" / "routing.md").write_text(
        "Gateway routing remains disabled.\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    packet = _packet(tmp_path)
    store = ReceiptStore(":memory:")

    pack = autodev_run.compile_packet_context(
        packet,
        tmp_path,
        repository_instruction_paths=("AGENTS.md",),
        governing_doc_paths=("specs/routing.md",),
        relevant_example_paths=("tests/test_headroom.py",),
        symbol_relationship="check_headroom -> test_missing_is_unknown",
        token_budget=1024,
        store=store,
    )

    assert "return True, 100.0" in pack.compiled_prompt
    assert "Missing evidence remains unknown." in pack.compiled_prompt
    assert "Gateway routing remains disabled." in pack.compiled_prompt
    assert "uv\nrun\npytest" in pack.compiled_prompt
    assert pack.receipt.verify(pack)
    again = autodev_run.compile_packet_context(
        packet,
        tmp_path,
        repository_instruction_paths=("AGENTS.md",),
        governing_doc_paths=("specs/routing.md",),
        relevant_example_paths=("tests/test_headroom.py",),
        symbol_relationship="check_headroom -> test_missing_is_unknown",
        token_budget=1024,
    )
    assert again.digest == pack.digest
    assert again.canonical_json() == pack.canonical_json()
    receipts = store.query_receipts(scope="operational-loop")
    assert len(receipts) == 1
    payload: Mapping[str, Any] = receipts[0].payload
    assert payload["context_digest"] == pack.digest
    assert payload["compiled_prompt"] == "[REDACTED]"
