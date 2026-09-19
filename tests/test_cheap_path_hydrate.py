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
    included = {item.source_uri: item.source_digest for item in packed.included}
    assert included["docs/adr/ADR-001-hydrate.md"] == _digest(files["docs/adr/ADR-001-hydrate.md"])
    assert included["docs/architecture/decision.md"] == _digest(
        files["docs/architecture/decision.md"]
    )
    receipt = packed.to_dict()
    assert receipt["pack_digest"].startswith("sha256:")
    assert receipt["pack_state"] == "hydrated"
    assert {row["source_uri"] for row in receipt["included"]} >= {
        "docs/adr/ADR-001-hydrate.md",
        "docs/architecture/decision.md",
    }
    assert receipt["included_sources"] == receipt["included"]
    assert all("source_digest" in row for row in receipt["included_sources"])
    assert packed.included_sources == packed.included


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
    included = {item.source_uri for item in packed.included}
    assert "docs/adr/ADR-001-hydrate.md" in included
    assert packed.pack_digest.startswith("sha256:")
    assert packed.pack_state == "hydrated"
    assert packed.to_dict()["included_sources"]


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
    assert packed.included == ()
    assert packed.pack_state == "empty"
    receipt = packed.to_dict()
    assert receipt["pack_state"] == "empty"
    assert receipt["included_sources"] == []
    assert receipt["pack_digest"].startswith("sha256:")


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


def test_adr_and_architecture_included_when_large_adrs_would_starve_budget(tmp_path: Path) -> None:
    """BOD-99 / #516 QA: a normal ADR+architecture set must not be omissions-only.

    Alphabetically, every ``docs/adr/*`` policy unit sorts before architecture.
    Without reserved high-value slots, a handful of large ADRs exhaust a 4096
    token budget and architecture lands only as ``input_budget_exhausted``.
    """
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    adr_body = ("ADR body line with enough tokens to pressure the pack. " * 80) + f"{ADR_TOKEN}\n"
    arch_body = f"# Architecture\n\nSmall architecture token {ARCH_TOKEN}.\n"
    readme_body = f"# Fixture\n\n{README_TOKEN}\n"
    misc_body = ("misc documentation that should yield to thesis roots. " * 400) + f"{DOCS_TOKEN}\n"
    files = {
        "README.md": readme_body,
        "docs/architecture/decision.md": arch_body,
        "docs/guide.md": misc_body,
    }
    for index in range(1, 9):
        relative = f"docs/adr/ADR-00{index}-hydrate.md"
        files[relative] = f"# ADR-00{index}\n\n{adr_body}"
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    packed = build_cheap_path_context_pack(
        "hydrate architecture ADR and project docs",
        candidate_id="openrouter/free-model",
        token_budget=4096,
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    included = {item.source_uri: item.source_digest for item in packed.included}
    assert any(uri.startswith("docs/adr/") for uri in included), included
    assert "docs/architecture/decision.md" in included
    assert included["docs/architecture/decision.md"] == _digest(arch_body)
    assert "README.md" in included
    assert ADR_TOKEN in packed.compiled_prompt
    assert ARCH_TOKEN in packed.compiled_prompt
    assert MISSING_TOKEN not in packed.compiled_prompt
    budget_omissions = [
        item for item in packed.omissions if item.reason == "input_budget_exhausted"
    ]
    assert packed.included, "omissions-only pack is a QA fail when roots exist on disk"
    assert not (budget_omissions and not packed.included), (
        "gathered thesis docs must not all be budget omissions"
    )
    receipt = packed.to_dict()
    assert receipt["pack_digest"].startswith("sha256:")
    assert receipt["included"]
    assert all("source_uri" in row and "source_digest" in row for row in receipt["included"])


def test_oversize_unit_omits_with_budget_reason_but_adr_still_included(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    adr = tmp_path / "docs" / "adr" / "ADR-001-hydrate.md"
    arch = tmp_path / "docs" / "architecture" / "decision.md"
    huge = tmp_path / "docs" / "guide.md"
    adr.write_text(f"Present ADR {ADR_TOKEN}\n", encoding="utf-8")
    arch.write_text(f"Architecture {ARCH_TOKEN}\n", encoding="utf-8")
    huge.write_text("OVERSIZE " * 4000, encoding="utf-8")

    packed = build_cheap_path_context_pack(
        "use the ADR",
        candidate_id="openrouter/free-model",
        token_budget=120,
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    included = {item.source_uri for item in packed.included}
    named = {item.name: item.reason for item in packed.omissions}
    assert "docs/adr/ADR-001-hydrate.md" in included
    assert ADR_TOKEN in packed.compiled_prompt
    assert packed.included, "normal ADR set must not be omissions-only"
    assert named.get("docs/guide.md") == "input_budget_exhausted"
    assert packed.pack_digest.startswith("sha256:")
    assert MISSING_TOKEN not in packed.compiled_prompt
    assert "invented" not in packed.compiled_prompt.lower()
    assert packed.pack_state == "hydrated"
    assert packed.to_dict()["included_sources"]


def test_budget_that_only_fits_one_high_value_class_is_partial(tmp_path: Path) -> None:
    """BOD-106: ADR+architecture on disk but budget fits one class → partial, not hydrated."""
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    body = ("HIGH-VALUE CLASS BODY " * 120) + "\n"
    adr = tmp_path / "docs" / "adr" / "ADR-001-hydrate.md"
    arch = tmp_path / "docs" / "architecture" / "decision.md"
    adr.write_text(f"# ADR\n\n{body}{ADR_TOKEN}\n", encoding="utf-8")
    arch.write_text(f"# Architecture\n\n{body}{ARCH_TOKEN}\n", encoding="utf-8")

    packed = build_cheap_path_context_pack(
        "hydrate",
        candidate_id="openrouter/free-model",
        token_budget=800,
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    included = {item.source_uri for item in packed.included}
    budget_omissions = {
        item.name for item in packed.omissions if item.reason == "input_budget_exhausted"
    }
    assert packed.included, "one class should still include"
    assert packed.pack_state == "partial"
    assert packed.pack_state != "hydrated"
    assert packed.to_dict()["pack_state"] == "partial"
    assert packed.to_dict()["included_sources"] == packed.to_dict()["included"]
    has_adr = any(uri.startswith("docs/adr/") for uri in included)
    has_arch = "docs/architecture/decision.md" in included
    assert has_adr ^ has_arch, included
    assert budget_omissions
    assert packed.pack_digest.startswith("sha256:")


def test_hydrate_compiler_error_stamps_failed(tmp_path: Path, monkeypatch: object) -> None:
    _plant(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        from verdict.context_pack import ContextContractError

        raise ContextContractError("forced compiler failure")

    monkeypatch.setattr("verdict.context_pack.ContextPackCompiler.compile_units", boom)
    packed = build_cheap_path_context_pack(
        "hydrate architecture ADR",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert packed.pack_state == "failed"
    assert packed.included_sources == ()
    receipt = packed.to_dict()
    assert receipt["pack_state"] == "failed"
    assert receipt["included_sources"] == []
    assert any(item.reason == "compiler_error" for item in packed.omissions)
    assert "hydrate architecture ADR" in packed.compiled_prompt
    assert packed.pack_digest.startswith("sha256:")


# --- BOD-110: task- and requirement-complete hydration -----------------------


def test_unicode_content_honors_max_file_bytes_by_bytes(tmp_path: Path) -> None:
    """``max_file_bytes`` is a byte limit; multibyte text must not exceed it."""
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    adr = tmp_path / "docs" / "adr" / "ADR-001-unicode.md"
    # Each "é" is 2 bytes in UTF-8: 3000 chars == 6000 bytes.
    adr.write_text("é" * 3000, encoding="utf-8")

    gathered = gather_cheap_path_units(
        "unicode", workspace_root=tmp_path, roots=("docs/adr",), mcp_root="", max_file_bytes=1000
    )
    unit = next(item for item in gathered.units if item.source_uri == "docs/adr/ADR-001-unicode.md")
    encoded = unit.content.encode("utf-8")
    assert len(encoded) <= 1000
    assert len(unit.content) < 1000, "char count must not be used as the byte limit"
    # Never split a code point: the bounded content must still decode strictly.
    encoded.decode("utf-8")


def test_truncate_utf8_never_splits_a_code_point() -> None:
    from verdict.context_hydrate import truncate_utf8

    assert truncate_utf8("abc", 10) == "abc"
    assert truncate_utf8("é" * 3, 3) == "é"  # 3 bytes fits one 2-byte char, not one-and-a-half
    assert truncate_utf8("日本語", 4) == "日"
    assert truncate_utf8("anything", 0) == ""


def test_oversized_task_is_failed_not_hydrated(tmp_path: Path) -> None:
    """Task instructions dropped for budget make the pack ``failed`` — never hydrated."""
    _plant(tmp_path)
    huge_task = "REPEAT THIS INSTRUCTION " * 2000

    packed = build_cheap_path_context_pack(
        huge_task,
        candidate_id="openrouter/free-model",
        token_budget=200,
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert packed.task_complete is False
    assert packed.pack_state == "failed"
    named = {item.name: item.reason for item in packed.omissions}
    assert named["urn:verdict:task"] == "task_instructions_omitted"
    receipt = packed.to_dict()
    assert receipt["task_complete"] is False
    assert receipt["pack_state"] == "failed"
    assert "REPEAT THIS INSTRUCTION" not in packed.compiled_prompt


def test_task_relevant_adr_omitted_is_partial_even_when_another_adr_landed(tmp_path: Path) -> None:
    """A small unrelated ADR must not let a budget-omitted task-relevant ADR read hydrated."""
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    small = tmp_path / "docs" / "adr" / "ADR-001-naming.md"
    relevant = tmp_path / "docs" / "adr" / "ADR-002-billing-ledger.md"
    arch = tmp_path / "docs" / "architecture" / "overview.md"
    small.write_text("# ADR-001 Naming\n\nUse kebab-case.\n", encoding="utf-8")
    relevant.write_text(
        "# ADR-002 Billing ledger\n\n" + ("Billing ledger invariant text. " * 200) + "\n",
        encoding="utf-8",
    )
    arch.write_text("# Architecture\n\nOne control plane.\n", encoding="utf-8")

    packed = build_cheap_path_context_pack(
        "fix the billing ledger reconciliation bug",
        candidate_id="openrouter/free-model",
        token_budget=400,
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    included = {item.source_uri for item in packed.included}
    assert "docs/adr/ADR-001-naming.md" in included, "class-level ADR coverage is present"
    assert "docs/adr/ADR-002-billing-ledger.md" not in included
    assert "docs/adr/ADR-002-billing-ledger.md" in packed.required_sources
    assert packed.missing_required_sources == ("docs/adr/ADR-002-billing-ledger.md",)
    assert packed.pack_state == "partial"
    receipt = packed.to_dict()
    assert receipt["missing_required_sources"] == ["docs/adr/ADR-002-billing-ledger.md"]
    named = {item.name: item.reason for item in packed.omissions}
    assert named["docs/adr/ADR-002-billing-ledger.md"] == "input_budget_exhausted"


def test_task_relevant_adr_included_is_hydrated(tmp_path: Path) -> None:
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "ADR-002-billing-ledger.md").write_text(
        "# ADR-002 Billing ledger\n\nLedger invariant.\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "architecture" / "overview.md").write_text(
        "# Architecture\n\nOne control plane.\n", encoding="utf-8"
    )
    packed = build_cheap_path_context_pack(
        "fix the billing ledger reconciliation bug",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert packed.required_sources == ("docs/adr/ADR-002-billing-ledger.md",)
    assert packed.missing_required_sources == ()
    assert packed.task_complete is True
    assert packed.pack_state == "hydrated"


def test_task_relevant_adr_beyond_gather_cap_is_named_and_makes_pack_partial(
    tmp_path: Path,
) -> None:
    """BOD-110 gap: a required ADR the per-root unit cap never gathered is still required."""
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / "docs" / "architecture" / "overview.md").write_text(
        "# Architecture\n\nOne control plane.\n", encoding="utf-8"
    )
    # One more task-matching ADR than the default per-root cap allows.
    from verdict.context_hydrate import DEFAULT_MAX_UNITS_PER_ROOT

    count = DEFAULT_MAX_UNITS_PER_ROOT + 1
    for index in range(count):
        (tmp_path / "docs" / "adr" / f"ADR-{index:03d}-billing-ledger-part{index}.md").write_text(
            f"# ADR-{index:03d} Billing ledger part {index}\n\nLedger invariant {index}.\n",
            encoding="utf-8",
        )

    gathered = gather_cheap_path_units(
        "fix the billing ledger reconciliation bug",
        workspace_root=tmp_path,
        roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    adr_units = [u.source_uri for u in gathered.units if u.source_uri.startswith("docs/adr/")]
    assert len(adr_units) == DEFAULT_MAX_UNITS_PER_ROOT
    assert len(gathered.required_uris) == count, "every matching ADR is required, gathered or not"
    capped = {o.name: o.reason for o in gathered.omissions if o.reason == "unit_cap_exceeded"}
    assert len(capped) == 1
    (capped_uri,) = capped
    assert capped_uri in gathered.required_uris
    assert capped_uri not in adr_units

    packed = build_cheap_path_context_pack(
        "fix the billing ledger reconciliation bug",
        candidate_id="openrouter/free-model",
        token_budget=200_000,
        workspace_root=tmp_path,
        workspace_roots=DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
    )
    assert packed.missing_required_sources == (capped_uri,)
    assert packed.pack_state == "partial"
    named = {item.name: item.reason for item in packed.omissions}
    assert named[capped_uri] == "unit_cap_exceeded"
