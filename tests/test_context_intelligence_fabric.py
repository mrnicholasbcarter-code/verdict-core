"""BOD-123 Wave-1: native Context Intelligence Fabric baseline (no MCP)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from verdict.context_intelligence import (
    WAVE1_NATIVE_CAPABILITIES,
    CapabilityCoverage,
    ContextQueryPlan,
    CoverageGap,
    FabricRetrieval,
    NativeCapabilityResolver,
    compile_pack,
    execute_context_query,
    plan_context_query,
)
from verdict.context_pack import ContextReceipt
from verdict.memory_plane import MemoryPlane, MemoryRecord

ALPHA_SRC = '''\
"""Fixture module for native symbol intelligence."""

def alpha_widget(value: int) -> int:
    """Return value doubled."""
    return value * 2


def caller_of_alpha() -> int:
    return alpha_widget(21)
'''

BETA_SRC = '''\
from fixture_pkg.alpha import alpha_widget


def use_alpha() -> int:
    return alpha_widget(3)
'''

ADR_TEXT = """# ADR-9001: Alpha Widget Boundary

Status: Accepted

## Decision

The alpha_widget function is the only public entry for doubling logic.
Constraint token: ADR_ALPHA_BOUNDARY_OK
"""


def _fresh_vps_fixture(tmp_path: Path) -> tuple[Path, MemoryPlane]:
    """Minimal repo proving native baseline without MCP/Serena."""
    root = tmp_path / "fresh_vps"
    pkg = root / "fixture_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "alpha.py").write_text(ALPHA_SRC, encoding="utf-8")
    (pkg / "beta.py").write_text(BETA_SRC, encoding="utf-8")
    adr = root / "docs" / "adr"
    adr.mkdir(parents=True)
    (adr / "9001-alpha-widget.md").write_text(ADR_TEXT, encoding="utf-8")
    (root / "README.md").write_text("# Fresh VPS fixture\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Agents\nUse alpha_widget for doubling.\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    (pkg / "alpha.py").write_text(
        ALPHA_SRC + "\n\ndef unused_helper() -> None:\n    pass\n",
        encoding="utf-8",
    )

    plane = MemoryPlane(root / "memory.db")
    plane.put(
        MemoryRecord(
            "mem-alpha-1",
            "decisions",
            "alpha_widget_policy",
            "Memory fact: prefer native alpha_widget; token MEM_ALPHA_OK",
            "local-test",
        )
    )
    return root, plane


def test_wave1_capability_contracts_are_brand_free() -> None:
    assert "code.symbols" in WAVE1_NATIVE_CAPABILITIES
    assert "code.definitions" in WAVE1_NATIVE_CAPABILITIES
    assert "code.references" in WAVE1_NATIVE_CAPABILITIES
    assert "git.diff" in WAVE1_NATIVE_CAPABILITIES
    assert "repo.state" in WAVE1_NATIVE_CAPABILITIES
    assert "docs.project" in WAVE1_NATIVE_CAPABILITIES
    assert "memory.search" in WAVE1_NATIVE_CAPABILITIES
    assert "task.requirements" in WAVE1_NATIVE_CAPABILITIES
    assert "task.proof" in WAVE1_NATIVE_CAPABILITIES
    for cap in WAVE1_NATIVE_CAPABILITIES:
        assert not cap.lower().startswith("serena")
        assert "mcp" not in cap.lower()


def test_resolver_always_offers_native_baseline() -> None:
    resolver = NativeCapabilityResolver()
    for capability_id in (
        "code.symbols",
        "code.definitions",
        "code.references",
        "docs.project",
        "memory.search",
        "git.diff",
        "repo.state",
        "task.requirements",
        "task.proof",
    ):
        provider = resolver.resolve(capability_id)
        assert provider is not None
        assert provider.provider_id == "native.verdict"
        assert capability_id in provider.capabilities


def test_missing_external_capability_is_named_gap_not_total_failure(tmp_path: Path) -> None:
    root, plane = _fresh_vps_fixture(tmp_path)
    try:
        plan = plan_context_query(
            task="Rename alpha_widget carefully",
            acceptance_criteria=("Keep doubling semantics",),
            proof_criteria=("tests pass",),
            errors=(),
            token_budget=2000,
            extra_capabilities=("docs.library",),
        )
        result = execute_context_query(plan, repo_root=root, plane=plane)
        assert isinstance(result, FabricRetrieval)
        assert result.units  # native baseline still produced units
        gaps = {gap.capability_id: gap.reason for gap in result.coverage.omitted}
        assert "docs.library" in gaps
        assert gaps["docs.library"] in {"provider_unavailable", "no_provider", "coverage_gap"}
    finally:
        plane.close()


def test_plan_is_capability_oriented_not_serena() -> None:
    plan = plan_context_query(
        task="Fix alpha_widget callers",
        acceptance_criteria=("Update all references",),
        proof_criteria=("pytest green",),
        errors=("NameError: alpha_widget",),
        token_budget=1500,
        target_symbols=("alpha_widget",),
    )
    assert isinstance(plan, ContextQueryPlan)
    requested = {req.capability_id for req in plan.requests}
    assert "code.definitions" in requested
    assert "code.references" in requested
    assert "docs.project" in requested or "memory.search" in requested
    assert all("serena" not in req.capability_id.lower() for req in plan.requests)


def test_bounded_expansion_stops_before_repo_dump(tmp_path: Path) -> None:
    root, plane = _fresh_vps_fixture(tmp_path)
    noise = root / "noise"
    noise.mkdir()
    for index in range(40):
        (noise / f"noise_{index:02d}.py").write_text(
            f"def noise_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    try:
        plan = plan_context_query(
            task="Inspect alpha_widget",
            acceptance_criteria=("Find definition",),
            proof_criteria=("bounded pack",),
            errors=(),
            token_budget=800,
            target_symbols=("alpha_widget",),
            max_units=6,
            max_expansion_depth=1,
        )
        result = execute_context_query(plan, repo_root=root, plane=plane)
        assert len(result.units) <= plan.max_units
        assert result.stop_reason in {"budget", "max_units", "satisfied", "depth_limit"}
        uris = "\n".join(unit.source_uri for unit in result.units)
        assert "noise_00.py" not in uris
        assert result.file_count >= 20
        assert len(result.units) / max(result.file_count, 1) <= 0.25
    finally:
        plane.close()


def test_fresh_vps_no_mcp_retrieves_definition_reference_adr_memory(tmp_path: Path) -> None:
    # Explicitly clear MCP-ish env so the fixture mimics a bare VPS.
    for key in list(os.environ):
        if "MCP" in key.upper() or "SERENA" in key.upper():
            os.environ.pop(key, None)

    root, plane = _fresh_vps_fixture(tmp_path)
    try:
        plan = plan_context_query(
            task="Rename alpha_widget and preserve ADR + memory constraints",
            acceptance_criteria=(
                "Update definition and references",
                "Honor ADR_ALPHA_BOUNDARY_OK",
            ),
            proof_criteria=("MEM_ALPHA_OK retained", "pytest green"),
            errors=(),
            token_budget=2500,
            max_units=16,
            target_symbols=("alpha_widget",),
        )
        result = execute_context_query(plan, repo_root=root, plane=plane)
        joined = "\n".join(unit.content for unit in result.units)
        caps_used = set(result.coverage.used)

        assert "def alpha_widget" in joined
        assert "code.definitions" in caps_used or "code.symbols" in caps_used
        assert "alpha_widget" in joined
        assert any(
            "beta.py" in unit.source_uri or "caller_of_alpha" in unit.content
            or "use_alpha" in unit.content
            for unit in result.units
        ), "expected at least one reference neighborhood unit"
        assert "ADR_ALPHA_BOUNDARY_OK" in joined
        assert "MEM_ALPHA_OK" in joined
        assert "docs.project" in caps_used
        assert "memory.search" in caps_used

        # Provenance / authority / freshness present on units
        for unit in result.units:
            assert unit.source_uri
            assert unit.source_digest.startswith("sha256:")
            assert unit.observed_at
            assert unit.authority
            assert unit.trust

        pack, state = compile_pack(
            result.units,
            token_budget=2000,
            required_fact="ADR_ALPHA_BOUNDARY_OK",
            candidate_id="free/test",
        )
        receipt = ContextReceipt.from_pack(pack)
        covered = result.attach_coverage(receipt)
        assert isinstance(covered.capability_coverage, dict)
        assert set(covered.capability_coverage["requested"]) >= {
            "code.definitions",
            "docs.project",
            "memory.search",
        }
        assert "available" in covered.capability_coverage
        assert "used" in covered.capability_coverage
        assert "omitted" in covered.capability_coverage
        assert state.required_fact_kept is True
    finally:
        plane.close()


def test_native_symbols_definitions_references_on_fixture(tmp_path: Path) -> None:
    root, plane = _fresh_vps_fixture(tmp_path)
    try:
        resolver = NativeCapabilityResolver()
        symbols = resolver.resolve("code.symbols")
        assert symbols is not None
        sym_result = symbols.provide(
            capability_id="code.symbols",
            query="alpha_widget",
            repo_root=root,
            max_units=8,
            hints={"symbols": ("alpha_widget",)},
        )
        assert any("alpha_widget" in unit.content for unit in sym_result.units)

        definitions = resolver.resolve("code.definitions")
        assert definitions is not None
        def_result = definitions.provide(
            capability_id="code.definitions",
            query="alpha_widget",
            repo_root=root,
            max_units=4,
            hints={"symbols": ("alpha_widget",)},
        )
        assert any("def alpha_widget" in unit.content for unit in def_result.units)
        assert all(unit.authority == "native-code" for unit in def_result.units)

        references = resolver.resolve("code.references")
        assert references is not None
        ref_result = references.provide(
            capability_id="code.references",
            query="alpha_widget",
            repo_root=root,
            max_units=8,
            hints={"symbols": ("alpha_widget",)},
        )
        assert len(ref_result.units) >= 1
        assert any(
            "alpha_widget(" in unit.content and "def alpha_widget" not in unit.content
            for unit in ref_result.units
        ) or any("use_alpha" in unit.content or "caller_of_alpha" in unit.content for unit in ref_result.units)
    finally:
        plane.close()


def test_docs_project_retrieves_adr(tmp_path: Path) -> None:
    root, plane = _fresh_vps_fixture(tmp_path)
    try:
        resolver = NativeCapabilityResolver()
        docs = resolver.resolve("docs.project")
        assert docs is not None
        result = docs.provide(
            capability_id="docs.project",
            query="alpha_widget ADR boundary",
            repo_root=root,
            max_units=4,
        )
        assert any("ADR_ALPHA_BOUNDARY_OK" in unit.content for unit in result.units)
        assert any("docs/adr" in unit.source_uri or "9001" in unit.source_uri for unit in result.units)
    finally:
        plane.close()


def test_memory_search_capability(tmp_path: Path) -> None:
    root, plane = _fresh_vps_fixture(tmp_path)
    try:
        resolver = NativeCapabilityResolver()
        memory = resolver.resolve("memory.search")
        assert memory is not None
        result = memory.provide(
            capability_id="memory.search",
            query="alpha_widget native",
            repo_root=root,
            max_units=4,
            plane=plane,
        )
        assert any("MEM_ALPHA_OK" in unit.content for unit in result.units)
    finally:
        plane.close()


def test_git_diff_and_repo_state(tmp_path: Path) -> None:
    root, plane = _fresh_vps_fixture(tmp_path)
    try:
        resolver = NativeCapabilityResolver()
        diff = resolver.resolve("git.diff")
        assert diff is not None
        diff_result = diff.provide(
            capability_id="git.diff",
            query="alpha",
            repo_root=root,
            max_units=2,
        )
        assert diff_result.units or diff_result.gap is not None
        if diff_result.units:
            assert any("unused_helper" in unit.content or "alpha.py" in unit.content for unit in diff_result.units)

        state = resolver.resolve("repo.state")
        assert state is not None
        state_result = state.provide(
            capability_id="repo.state",
            query="",
            repo_root=root,
            max_units=1,
        )
        assert state_result.units
        joined = state_result.units[0].content
        assert "branch" in joined.lower() or "HEAD" in joined or "revision" in joined.lower()
    finally:
        plane.close()


def test_task_requirements_and_proof_units() -> None:
    resolver = NativeCapabilityResolver()
    req = resolver.resolve("task.requirements")
    proof = resolver.resolve("task.proof")
    assert req is not None and proof is not None
    req_result = req.provide(
        capability_id="task.requirements",
        query="Rename alpha_widget",
        repo_root=Path("."),
        max_units=2,
        hints={
            "acceptance_criteria": ("Update references",),
            "task": "Rename alpha_widget",
        },
    )
    proof_result = proof.provide(
        capability_id="task.proof",
        query="Rename alpha_widget",
        repo_root=Path("."),
        max_units=2,
        hints={"proof_criteria": ("pytest green", "MEM_ALPHA_OK retained")},
    )
    assert req_result.units
    assert "Update references" in req_result.units[0].content
    assert proof_result.units
    assert "pytest green" in proof_result.units[0].content


def test_capability_coverage_roundtrip() -> None:
    coverage = CapabilityCoverage(
        requested=("code.definitions", "docs.library"),
        available=("code.definitions",),
        used=("code.definitions",),
        omitted=(CoverageGap("docs.library", "provider_unavailable"),),
    )
    payload = coverage.to_dict()
    assert payload["requested"] == ["code.definitions", "docs.library"]
    assert payload["omitted"][0]["reason"] == "provider_unavailable"
