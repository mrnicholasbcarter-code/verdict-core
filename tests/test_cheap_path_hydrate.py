"""Cheap-path rich hydrate: gather real provenance units, never invent."""

from __future__ import annotations

import hashlib
from pathlib import Path

from verdict.context_hydrate import DEFAULT_CONTEXT_ROOTS, gather_cheap_path_units
from verdict.free_tier_admit import build_cheap_path_context_pack

ADR_TOKEN = "ALPHA-ADR-TOKEN"
ARCH_TOKEN = "ALPHA-ARCH-TOKEN"
README_TOKEN = "ALPHA-README-TOKEN"
DOCS_TOKEN = "ALPHA-DOCS-TOKEN"
MISSING_TOKEN = "ALPHA-MISSING-SHOULD-NEVER-APPEAR"
MCP_TOKEN = "ALPHA-MCP-TOKEN"


def _digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _plant(root: Path) -> dict[str, str]:
    """Write a small workspace with provenance-bearing fixture files."""
    (root / "docs" / "adr").mkdir(parents=True)
    (root / "docs" / "architecture").mkdir(parents=True)
    files = {
        "README.md": f"# Fixture project\n\nThe readme token is {README_TOKEN}.\n",
        "docs/adr/ADR-001-hydrate.md": (
            f"# ADR-001 Hydrate\n\nGoverning decision token: {ADR_TOKEN}.\n"
        ),
        "docs/architecture/decision.md": (f"# Architecture\n\nArchitecture token: {ARCH_TOKEN}.\n"),
        "docs/guide.md": f"# Guide\n\nDocs token: {DOCS_TOKEN}.\n",
    }
    for relative, content in files.items():
        path = root / relative
        path.write_text(content, encoding="utf-8")
    return files


def test_fixture_files_become_pack_units_with_provenance(tmp_path: Path) -> None:
    files = _plant(tmp_path)
    packed = build_cheap_path_context_pack(
        "hydrate architecture ADR and project docs",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    by_uri = {unit.source_uri: unit for unit in packed.units}
    for relative, content in files.items():
        assert relative in by_uri, f"missing unit for {relative}"
        unit = by_uri[relative]
        assert unit.source_digest == _digest(content)
        assert unit.source_uri == relative
        assert content.strip() in packed.compiled_prompt
    assert ADR_TOKEN in packed.compiled_prompt
    assert ARCH_TOKEN in packed.compiled_prompt
    assert README_TOKEN in packed.compiled_prompt
    assert DOCS_TOKEN in packed.compiled_prompt
    assert MISSING_TOKEN not in packed.compiled_prompt
    named = {item.name: item.reason for item in packed.omissions}
    assert "mcp" not in named


def test_missing_root_is_named_omission_not_invented(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    adr = tmp_path / "docs" / "adr" / "ADR-001-hydrate.md"
    adr.write_text(f"Present ADR {ADR_TOKEN}\n", encoding="utf-8")
    packed = build_cheap_path_context_pack(
        "use the ADR",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=("docs/adr", "docs/missing"),
        mcp_root="",
    )
    named = {item.name: item.reason for item in packed.omissions}
    assert named.get("docs/missing") == "source_missing"
    assert ADR_TOKEN in packed.compiled_prompt
    assert MISSING_TOKEN not in packed.compiled_prompt
    assert "invented" not in packed.compiled_prompt.lower()
    uris = {unit.source_uri for unit in packed.units}
    assert "docs/adr/ADR-001-hydrate.md" in uris
    assert not any("missing" in uri for uri in uris)


def test_hydrate_digest_is_stable_for_identical_workspace(tmp_path: Path) -> None:
    _plant(tmp_path)
    first = build_cheap_path_context_pack(
        "hydrate architecture ADR and project docs",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    second = build_cheap_path_context_pack(
        "hydrate architecture ADR and project docs",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert first.pack_digest == second.pack_digest
    assert first.pack_digest.startswith("sha256:")
    assert first.compiled_prompt == second.compiled_prompt


def test_empty_workspace_fails_soft_with_named_omissions(tmp_path: Path) -> None:
    packed = build_cheap_path_context_pack(
        "still execute this task",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert "still execute this task" in packed.compiled_prompt
    named = {item.name: item.reason for item in packed.omissions}
    assert named["docs"] == "source_missing"
    assert named["docs/architecture"] == "source_missing"
    assert named["docs/adr"] == "source_missing"
    assert MISSING_TOKEN not in packed.compiled_prompt
    assert packed.pack_digest.startswith("sha256:")


def test_mcp_not_configured_is_not_faked(tmp_path: Path) -> None:
    _plant(tmp_path)
    packed = build_cheap_path_context_pack(
        "hydrate",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert all(not unit.source_uri.startswith("mcp:") for unit in packed.units)
    assert all(item.name != "mcp" for item in packed.omissions)
    assert MCP_TOKEN not in packed.compiled_prompt


def test_mcp_configured_but_missing_is_named_omission(tmp_path: Path) -> None:
    _plant(tmp_path)
    packed = build_cheap_path_context_pack(
        "hydrate",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root=tmp_path / "no-such-mcp",
    )
    named = {item.name: item.reason for item in packed.omissions}
    assert named.get("mcp") == "source_missing"
    assert MCP_TOKEN not in packed.compiled_prompt
    assert all(not unit.source_uri.startswith("mcp:") for unit in packed.units)


def test_mcp_present_becomes_provenance_unit(tmp_path: Path) -> None:
    _plant(tmp_path)
    mcp = tmp_path / "mcp-export"
    mcp.mkdir()
    (mcp / "note.md").write_text(f"MCP retrieved note {MCP_TOKEN}\n", encoding="utf-8")
    packed = build_cheap_path_context_pack(
        "hydrate",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root=mcp,
    )
    mcp_units = [unit for unit in packed.units if unit.source_uri.startswith("mcp:")]
    assert mcp_units
    assert MCP_TOKEN in packed.compiled_prompt
    assert mcp_units[0].source_digest == _digest(f"MCP retrieved note {MCP_TOKEN}\n")
    assert all(item.name != "mcp" or item.reason != "source_missing" for item in packed.omissions)


def test_gather_uses_configured_roots_only(tmp_path: Path) -> None:
    _plant(tmp_path)
    gathered = gather_cheap_path_units(
        "hydrate", workspace_root=tmp_path, roots=("docs/adr",), mcp_root=""
    )
    uris = {unit.source_uri for unit in gathered.units}
    assert "docs/adr/ADR-001-hydrate.md" in uris
    assert "docs/architecture/decision.md" not in uris
    assert all(item.name != "docs/architecture" for item in gathered.omissions)
