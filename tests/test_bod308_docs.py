"""BOD-308 docs artifacts: OpenSpec change conformance and golden-path guide consistency.

Validates that the bod-308-partial-salvage OpenSpec change is present and passes
the vendored verdict-change-v1 validator, and that the orchestration golden-path
guide documents salvage preservation, retry restoration, content-free receipt
events and the rejection boundaries using the same names as the change.
"""

from __future__ import annotations

from pathlib import Path

from verdict.openspec_vendor.validate_openspec_change import validate_change

REPO_ROOT = Path(__file__).parent.parent
CHANGE_DIR = REPO_ROOT / "openspec" / "changes" / "bod-308-partial-salvage"
GUIDE = REPO_ROOT / "docs" / "guides" / "orchestration-golden-path.md"

REQUIRED_ARTIFACTS = ("proposal.md", "design.md", "tasks.md", "specs/partial-salvage/spec.md")


def test_openspec_artifacts_present() -> None:
    for rel in REQUIRED_ARTIFACTS:
        assert (CHANGE_DIR / rel).is_file(), f"missing OpenSpec artifact: {rel}"


def test_openspec_change_passes_vendored_validator() -> None:
    errors = validate_change(CHANGE_DIR)
    assert not errors, "verdict-change-v1 validation failed: " + "; ".join(
        f"[{e.artifact}] {e.reason}" for e in errors
    )


def test_spec_delta_covers_required_boundaries() -> None:
    spec = (CHANGE_DIR / "specs" / "partial-salvage" / "spec.md").read_text(encoding="utf-8")
    for needle in (
        "## ADDED Requirements",
        "content-free",
        "ownership contract",
        "Credential-bearing",
        "fault-injected",
    ):
        assert needle in spec, f"spec delta missing: {needle!r}"


def test_tasks_name_focused_tests_and_production_caller() -> None:
    tasks = (CHANGE_DIR / "tasks.md").read_text(encoding="utf-8")
    assert "tests/test_orch_salvage.py" in tasks
    assert "tests/test_bod308_docs.py" in tasks
    assert "verdict/orchestration/runtime.py" in tasks
    assert "verdict/orchestration/salvage.py" in tasks


def test_guide_documents_salvage_semantics() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    flat = " ".join(guide.split())
    assert "## Partial-work salvage on retry" in guide
    # Preservation and same-node restoration semantics.
    assert "before the failed worktree is removed" in flat
    assert "SAME node's next attempt worktree" in flat
    # Content-free handoff evidence in the existing event log.
    assert "`salvage` event" in flat
    assert "content-free" in flat
    assert "File content never enters the event log" in flat
    # Rejection boundaries.
    assert "outside the node's ownership contract" in flat
    assert "credential-bearing files are rejected whole" in flat.lower()
    assert "fault-injected attempts" in flat.lower()
    assert "no-change safe" in flat


def test_guide_and_change_agree_on_owned_modules() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    proposal = (CHANGE_DIR / "proposal.md").read_text(encoding="utf-8")
    for module in ("verdict/orchestration/runtime.py", "verdict/orchestration/salvage.py"):
        assert module in guide, f"guide does not cite {module}"
        assert module in proposal, f"proposal does not cite {module}"
    assert "openspec/changes/bod-308-partial-salvage" in guide
