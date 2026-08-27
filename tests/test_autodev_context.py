"""Focused contract tests for the deterministic US1 worker context pack.

These tests intentionally target the thin autodev seam that T015 will add over
``ContextPackCompiler``.  They describe the worker-facing contract without
introducing a second context representation or retrieval framework.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

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
        "relevant_examples": ("tests/test_headroom.py::test_absent_endpoint_is_unknown",),
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
        decision.action == "exclude" and decision.reason == "input_budget_exhausted"
        for decision in pack.decisions
    )


def _packet(repo: Path) -> ExecutionPacket:
    return ExecutionPacket(
        packet_id="headroom-unknown",
        packet_version=1,
        story_id="US1",
        story_version="1",
        source=capture_source_binding(
            repo, repository="git@example.test:verdict.git", lock_paths=()
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


def test_context_ablation_payload_requires_distinct_digests_and_blocks_denied_paths(
    tmp_path: Path,
) -> None:
    import subprocess

    from verdict.autodev_run import AutodevError, context_ablation_payload

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "owned.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    packet = _packet(tmp_path)
    pack_a = _compile_context(token_budget=512)
    pack_b = _compile_context(
        token_budget=48, owned_source={"verdict/headroom.py": "source line " * 400}
    )
    payload = context_ablation_payload(packet, pack_a, pack_b)
    assert payload["packet_integrity_digest"] == packet.integrity_digest
    assert payload["pack_a"]["context_digest"] != payload["pack_b"]["context_digest"]
    assert payload["unowned_paths_present"] is False
    assert payload["success_delta"] == "UNKNOWN"
    assert payload["verified_a"] is None
    with pytest.raises(AutodevError):
        context_ablation_payload(packet, pack_a, pack_a)
    leaked = _compile_context(owned_source={"verdict/cli.py": "secret\n"})
    with pytest.raises(AutodevError):
        context_ablation_payload(packet, pack_a, leaked)


def test_context_ablation_extra_symbol_unit_does_not_read_denied_paths(tmp_path: Path) -> None:
    import subprocess

    from verdict.autodev_run import context_ablation_payload

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "owned.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    packet = _packet(tmp_path)
    pack_a = _compile_context(symbol_relationship=None)
    pack_b = _compile_context(
        symbol_relationship="headroom.py:provider_headroom -> test_headroom.py"
    )
    payload = context_ablation_payload(packet, pack_a, pack_b)
    assert payload["pack_a"]["context_digest"] != payload["pack_b"]["context_digest"]
    assert payload["unowned_paths_present"] is False
    assert not any(
        "verdict/cli.py" in f"{unit.key}\n{unit.source_uri}"
        for unit in pack_b.units
        if unit.slot_type in {"evidence", "examples"}
    )
    unknown = context_ablation_payload(packet, pack_a, pack_b)
    assert unknown["success_delta"] == "UNKNOWN"
    improved = context_ablation_payload(
        packet, pack_a, pack_b, trusted_verified_a=False, trusted_verified_b=True
    )
    assert improved["success_delta"] == "improved"
    assert improved["verified_a"] is False


def _context_repo(tmp_path: Path) -> Path:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "verdict").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "verdict" / "headroom.py").write_text("def check_headroom():\n    return None\n")
    (tmp_path / "tests" / "test_headroom.py").write_text("def test_unknown(): ...\n")
    (tmp_path / "verdict" / "cli.py").write_text("DENIED_SECRET_MARKER = 1\n")
    (tmp_path / "AGENTS.md").write_text("Keep owned paths only.\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def test_compile_packet_context_skips_denied_paths_on_read(tmp_path: Path) -> None:
    repo = _context_repo(tmp_path)
    packet = _packet(repo)
    pack = autodev_run.compile_packet_context(
        packet, repo, governing_doc_paths=("verdict/cli.py", "AGENTS.md"), token_budget=1024
    )
    assert "DENIED_SECRET_MARKER" not in pack.compiled_prompt
    assert "Keep owned paths only." in pack.compiled_prompt


def test_context_ablation_inventories_unowned_basenames(tmp_path: Path) -> None:
    from verdict.autodev_run import AutodevError, context_ablation_payload

    packet = _packet(_context_repo(tmp_path))
    pack_a = _compile_context(token_budget=512)
    pack_unowned = _compile_context(owned_source={"escape.txt": "private\n"})
    payload = context_ablation_payload(packet, pack_a, pack_unowned)
    assert payload["unowned_paths_present"] is True
    assert "escape.txt" in payload["unowned_paths"]
    leaked = _compile_context(owned_source={"verdict/cli.py": "secret\n"})
    with pytest.raises(AutodevError):
        context_ablation_payload(packet, pack_a, leaked)


def test_unretrievable_context_source_is_recorded_as_an_omission(tmp_path: Path) -> None:
    """FR-032: a source that could not be read must be an explicit omission.

    Previously ``read_selected`` swallowed OSError/UnicodeDecodeError, so a missing ADR
    was indistinguishable from one that does not exist. A weak model given a package with
    silently-dropped governing context has no way to know what it is missing.
    """
    repo = _context_repo(tmp_path)
    packet = _packet(repo)
    pack = autodev_run.compile_packet_context(
        packet, repo, governing_doc_paths=("docs/adr/ADR-999-does-not-exist.md",), token_budget=1024
    )
    omissions = {
        decision.unit_id: decision.reason
        for decision in pack.decisions
        if decision.action == "exclude"
    }
    assert any("ADR-999" in unit_id for unit_id in omissions), omissions
    reason = next(r for u, r in omissions.items() if "ADR-999" in u)
    assert "absent" in reason.lower()


def test_unreadable_source_omission_is_distinguishable_from_absent(tmp_path: Path) -> None:
    """FR-032: 'absent' and 'unreadable' are different failures and must not collapse."""
    repo = _context_repo(tmp_path)
    binary = repo / "docs" / "binary.md"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\xff\xfe\x00\x00not utf8")
    packet = _packet(repo)
    pack = autodev_run.compile_packet_context(
        packet, repo, governing_doc_paths=("docs/binary.md",), token_budget=1024
    )
    reasons = {d.unit_id: d.reason for d in pack.decisions if d.action == "exclude"}
    reason = next((r for u, r in reasons.items() if "binary.md" in u), None)
    assert reason is not None, reasons
    assert "unreadable" in reason.lower()


def test_governing_adrs_are_discovered_by_default(tmp_path: Path) -> None:
    """FR-032: the ADR slot must not be empty just because no caller named paths.

    The delegation thesis rests on a weak model receiving decision rationale it cannot
    infer from source alone. Defaulting governing_doc_paths to () meant that slot was
    always empty in production.
    """
    repo = _context_repo(tmp_path)
    adr = repo / "docs" / "adr" / "ADR-001-example-decision.md"
    adr.parent.mkdir(parents=True, exist_ok=True)
    adr.write_text("# ADR-001\n\nGOVERNING_RATIONALE_MARKER\n", encoding="utf-8")
    pack = autodev_run.compile_packet_context(_packet(repo), repo, token_budget=4096)
    assert "GOVERNING_RATIONALE_MARKER" in pack.compiled_prompt
