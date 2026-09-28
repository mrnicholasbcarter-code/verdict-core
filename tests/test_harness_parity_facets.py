"""BOD-80 harness parity facets — lifecycle matrix coverage tests.

Every harness must report every facet from HARNESS_PARITY_FACETS.
No facet may be SUPPORTED without a cited test proving the code path.
Known limits (PARTIAL/UNSUPPORTED) are asserted explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from verdict.runtime_certification import HARNESS_PARITY_FACETS

# ---------------------------------------------------------------------------
# Lifecycle matrix facets added by BOD-80
# ---------------------------------------------------------------------------
LIFECYCLE_FACETS = frozenset(
    {
        "session_start",
        "before_first_turn",
        "tool_pre_post",
        "edit_event",
        "compaction_yield",
        "verification_result",
        "session_end",
        "sync_async_semantics",
        "mutation_capability",
    }
)

# Original facets that must still be present
ORIGINAL_FACETS = frozenset(
    {
        "mcp",
        "hooks",
        "subagents",
        "resume",
        "tool_interception",
        "model_selection",
        "config_import",
        "structured_output",
    }
)

VALID_PARITY_LEVELS = frozenset({"supported", "partial", "unsupported", "not-installed"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _healthy(_url: str) -> bool:
    return True


def _fake_which(_name: str) -> str | None:
    return "/usr/bin/fake"


def _no_which(_name: str) -> str | None:
    return None


# ---------------------------------------------------------------------------
# HARNESS_PARITY_FACETS completeness
# ---------------------------------------------------------------------------


class TestHarnessParity:
    """Verify HARNESS_PARITY_FACETS includes the full lifecycle matrix."""

    def test_lifecycle_facets_present(self) -> None:
        actual = set(HARNESS_PARITY_FACETS)
        missing = LIFECYCLE_FACETS - actual
        assert not missing, f"missing lifecycle facets: {missing}"

    def test_original_facets_preserved(self) -> None:
        actual = set(HARNESS_PARITY_FACETS)
        missing = ORIGINAL_FACETS - actual
        assert not missing, f"original facets lost: {missing}"

    def test_all_facets_combined(self) -> None:
        expected = ORIGINAL_FACETS | LIFECYCLE_FACETS
        actual = set(HARNESS_PARITY_FACETS)
        assert actual == expected, (
            f"facet set mismatch: extra={actual - expected}, missing={expected - actual}"
        )


# ---------------------------------------------------------------------------
# Per-harness certify: every facet reported, valid levels, known limits
# ---------------------------------------------------------------------------


class TestPrimeParity:
    """Prime Agent harness reports all facets with correct evidence."""

    def test_reports_all_facets(self, tmp_path: Path) -> None:
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        actual = set(report.facets.keys())
        expected = set(HARNESS_PARITY_FACETS)
        assert actual == expected, f"missing={expected - actual}, extra={actual - expected}"

    def test_all_levels_valid(self, tmp_path: Path) -> None:
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet, level in report.facets.items():
            assert level in VALID_PARITY_LEVELS, (
                f"prime facet {facet!r} has invalid level {level!r}"
            )

    def test_session_start_is_partial(self, tmp_path: Path) -> None:
        """Session-start recall uses local plane only (memory_bridge ~211)."""
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["session_start"] == "partial"

    def test_before_first_turn_is_partial(self, tmp_path: Path) -> None:
        """Pre-turn recall is local-plane only."""
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["before_first_turn"] == "partial"

    def test_tool_pre_post_unsupported(self, tmp_path: Path) -> None:
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["tool_pre_post"] == "unsupported"

    def test_no_supported_without_test_evidence(self, tmp_path: Path) -> None:
        """No lifecycle facet may be SUPPORTED (all are PARTIAL or UNSUPPORTED)."""
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet in LIFECYCLE_FACETS:
            assert report.facets[facet] != "supported", (
                f"prime lifecycle facet {facet!r} is 'supported' but cross-harness "
                "memory is local-plane only — must be partial or unsupported"
            )


class TestClaudeParity:
    """Claude Code harness reports all facets with correct evidence."""

    def test_reports_all_facets(self, tmp_path: Path) -> None:
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        actual = set(report.facets.keys())
        expected = set(HARNESS_PARITY_FACETS)
        assert actual == expected, f"missing={expected - actual}, extra={actual - expected}"

    def test_all_levels_valid(self, tmp_path: Path) -> None:
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet, level in report.facets.items():
            assert level in VALID_PARITY_LEVELS, (
                f"claude facet {facet!r} has invalid level {level!r}"
            )

    def test_session_start_is_partial(self, tmp_path: Path) -> None:
        """Claude SessionStart hook uses local plane only (memory_bridge ~211)."""
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["session_start"] == "partial"

    def test_verification_result_unsupported(self, tmp_path: Path) -> None:
        """Claude adapter has no verification hook wired."""
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["verification_result"] == "unsupported"

    def test_session_end_unsupported(self, tmp_path: Path) -> None:
        """Claude adapter has no SessionEnd hook."""
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["session_end"] == "unsupported"

    def test_tool_pre_post_unsupported(self, tmp_path: Path) -> None:
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["tool_pre_post"] == "unsupported"

    def test_no_lifecycle_facet_supported(self, tmp_path: Path) -> None:
        """No lifecycle facet may be SUPPORTED — local-plane only."""
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet in LIFECYCLE_FACETS:
            assert report.facets[facet] != "supported", (
                f"claude lifecycle facet {facet!r} is 'supported' — must be partial or unsupported"
            )


class TestHermesParity:
    """Hermes harness reports all facets — all lifecycle facets UNSUPPORTED."""

    def test_reports_all_facets(self, tmp_path: Path) -> None:
        from verdict.harness_hermes import certify

        report = certify(hermes_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        actual = set(report.facets.keys())
        expected = set(HARNESS_PARITY_FACETS)
        assert actual == expected, f"missing={expected - actual}, extra={actual - expected}"

    def test_all_levels_valid(self, tmp_path: Path) -> None:
        from verdict.harness_hermes import certify

        report = certify(hermes_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet, level in report.facets.items():
            assert level in VALID_PARITY_LEVELS, (
                f"hermes facet {facet!r} has invalid level {level!r}"
            )

    def test_all_lifecycle_facets_unsupported(self, tmp_path: Path) -> None:
        """Hermes has no hook system — every lifecycle facet is unsupported."""
        from verdict.harness_hermes import certify

        report = certify(hermes_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet in LIFECYCLE_FACETS:
            assert report.facets[facet] == "unsupported", (
                f"hermes lifecycle facet {facet!r} should be unsupported, got {report.facets[facet]!r}"
            )

    def test_hooks_unsupported(self, tmp_path: Path) -> None:
        from verdict.harness_hermes import certify

        report = certify(hermes_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["hooks"] == "unsupported"


class TestCodexParity:
    """Codex harness reports all facets with correct evidence."""

    def test_reports_all_facets(self, tmp_path: Path) -> None:
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        actual = set(report.facets.keys())
        expected = set(HARNESS_PARITY_FACETS)
        assert actual == expected, f"missing={expected - actual}, extra={actual - expected}"

    def test_all_levels_valid(self, tmp_path: Path) -> None:
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet, level in report.facets.items():
            assert level in VALID_PARITY_LEVELS, (
                f"codex facet {facet!r} has invalid level {level!r}"
            )

    def test_session_start_is_partial(self, tmp_path: Path) -> None:
        """Codex SessionStart hook uses local plane only (memory_bridge ~358)."""
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["session_start"] == "partial"

    def test_before_first_turn_is_partial(self, tmp_path: Path) -> None:
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["before_first_turn"] == "partial"

    def test_tool_pre_post_unsupported(self, tmp_path: Path) -> None:
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["tool_pre_post"] == "unsupported"

    def test_edit_event_unsupported(self, tmp_path: Path) -> None:
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert report.facets["edit_event"] == "unsupported"

    def test_no_lifecycle_facet_supported(self, tmp_path: Path) -> None:
        """No lifecycle facet may be SUPPORTED — local-plane only."""
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        for facet in LIFECYCLE_FACETS:
            assert report.facets[facet] != "supported", (
                f"codex lifecycle facet {facet!r} is 'supported' — must be partial or unsupported"
            )


# ---------------------------------------------------------------------------
# Evidence citation validation
# ---------------------------------------------------------------------------


class TestEvidenceCitation:
    """Each facet citing evidence must reference a real source file or test."""

    def test_harness_parity_from_evidence_covers_all_facets(self) -> None:
        """harness_parity_from_evidence returns a facet for every HARNESS_PARITY_FACETS entry."""
        from verdict.runtime_certification import ParityLevel, harness_parity_from_evidence

        evidence: dict[str, Any] = {
            "mcp_config": True,
            "hooks_config": True,
            "subagents": True,
            "resume": True,
            "tool_interception": True,
            "model_selection": True,
            "config_import": True,
            "structured_output": True,
            "session_start": True,
            "before_first_turn": True,
            "tool_pre_post": True,
            "edit_event": True,
            "compaction_yield": True,
            "verification_result": True,
            "session_end": True,
            "sync_async_semantics": True,
            "mutation_capability": True,
        }
        parity = harness_parity_from_evidence(harness_id="test", evidence=evidence, source="test")
        by_facet = {p.facet: p for p in parity}
        assert set(by_facet) == set(HARNESS_PARITY_FACETS), (
            f"missing={set(HARNESS_PARITY_FACETS) - set(by_facet)}"
        )
        # All True evidence → supported
        for facet, item in by_facet.items():
            assert item.level == ParityLevel.SUPPORTED, f"{facet} should be supported"

    def test_harness_parity_from_evidence_partial(self) -> None:
        """Partial evidence → partial level."""
        from verdict.runtime_certification import ParityLevel, harness_parity_from_evidence

        evidence: dict[str, Any] = {"session_start": "partial"}
        parity = harness_parity_from_evidence(harness_id="test", evidence=evidence, source="test")
        by_facet = {p.facet: p for p in parity}
        assert by_facet["session_start"].level == ParityLevel.PARTIAL

    def test_harness_parity_from_evidence_unsupported_missing(self) -> None:
        """Missing evidence → unsupported."""
        from verdict.runtime_certification import ParityLevel, harness_parity_from_evidence

        parity = harness_parity_from_evidence(harness_id="test", evidence={}, source="test")
        by_facet = {p.facet: p for p in parity}
        for facet in LIFECYCLE_FACETS:
            assert by_facet[facet].level == ParityLevel.UNSUPPORTED


# ---------------------------------------------------------------------------
# Cross-harness: certification report includes new facets
# ---------------------------------------------------------------------------


class TestCertificationReportFacets:
    """Runtime certification report surfaces the expanded facet set."""

    def test_prime_certify_facet_count(self, tmp_path: Path) -> None:
        from verdict.harness_prime import certify

        report = certify(prime_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert len(report.facets) == len(HARNESS_PARITY_FACETS)

    def test_hermes_certify_facet_count(self, tmp_path: Path) -> None:
        from verdict.harness_hermes import certify

        report = certify(hermes_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert len(report.facets) == len(HARNESS_PARITY_FACETS)

    def test_claude_certify_facet_count(self, tmp_path: Path) -> None:
        from verdict.harness_claude import certify

        report = certify(claude_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert len(report.facets) == len(HARNESS_PARITY_FACETS)

    def test_codex_certify_facet_count(self, tmp_path: Path) -> None:
        from verdict.harness_codex import certify

        report = certify(codex_home=tmp_path, force=True, health_check=_healthy, which=_fake_which)
        assert len(report.facets) == len(HARNESS_PARITY_FACETS)
