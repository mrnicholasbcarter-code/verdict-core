"""Versioned semantic capability vocabulary (BOD-87).

Capabilities are brand-free domain identifiers. Provider brands (Serena,
Context7, Codebase Memory, etc.) belong on provider descriptors — never in
capability ids.

This vocabulary extends the BOD-123 Wave-1 native baseline; it does not
replace Context Intelligence fabric contracts.
"""

from __future__ import annotations

from typing import Final

SEMANTIC_CAPABILITY_SCHEMA_VERSION: Final[str] = "semantic-capabilities/v1"

# Brand-free semantic capabilities. Domain first (code.*, docs.*, …).
SEMANTIC_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        # Code intelligence
        "code.symbols",
        "code.definitions",
        "code.references",
        "code.callers",
        "code.callees",
        "code.imports",
        "code.graph",
        "code.changed-neighborhood",
        # Tests / git / repo
        "tests.impact",
        "git.diff",
        "repo.state",
        # Docs
        "docs.project",
        "docs.library",
        "docs.lookup",
        # Memory / task
        "memory.search",
        "task.requirements",
        "task.proof",
        # Harness / routing (registry-ready; planners consume later)
        "models.list",
        "models.health",
        "tracker.issue.read",
        "tracker.issue.write",
        "vcs.pr.read",
        "vcs.pr.write",
        "reasoning.sequential",
        # Security boundary (BOD-126) — brand-free domain ids; scanners are providers
        "security.prompt_injection",
        "security.secrets",
        "security.agent_config",
        "security.mcp_config",
    }
)

# Forbidden substrings — provider brands must not appear in capability ids.
_BRAND_FRAGMENTS: Final[frozenset[str]] = frozenset(
    {
        "serena",
        "context7",
        "codebase-memory",
        "codebase_memory",
        "omniroute",
        "github",
        "linear",
        "mcp",
        "lsp",
        "tree-sitter",
        "treesitter",
        "native.verdict",
        "agentshield",
        "gitleaks",
        "semgrep",
    }
)

# Protocol-domain exceptions: substring matches a brand fragment but names the
# security *subject* (MCP config scanning), not a provider product brand.
_BRAND_FREE_ALLOWLIST: Final[frozenset[str]] = frozenset({"security.mcp_config"})


class SemanticCapabilityError(ValueError):
    """Raised when a capability id violates the brand-free contract."""


def is_semantic_capability(capability_id: str) -> bool:
    return capability_id in SEMANTIC_CAPABILITIES


def assert_brand_free_capability_id(capability_id: str) -> None:
    """Reject capability ids that embed provider brand names."""
    if not capability_id or not capability_id.strip():
        raise SemanticCapabilityError("capability_id is required")
    lowered = capability_id.lower().strip()
    if lowered in _BRAND_FREE_ALLOWLIST:
        if "." not in capability_id:
            raise SemanticCapabilityError(
                f"capability id must be domain.capability shaped: {capability_id!r}"
            )
        return
    if lowered.startswith("native."):
        raise SemanticCapabilityError(
            f"capability id must not use provider namespace: {capability_id!r}"
        )
    for fragment in _BRAND_FRAGMENTS:
        if fragment in lowered:
            raise SemanticCapabilityError(
                f"capability id embeds provider brand {fragment!r}: {capability_id!r}"
            )
    if "." not in capability_id:
        raise SemanticCapabilityError(
            f"capability id must be domain.capability shaped: {capability_id!r}"
        )


__all__ = [
    "SEMANTIC_CAPABILITIES",
    "SEMANTIC_CAPABILITY_SCHEMA_VERSION",
    "SemanticCapabilityError",
    "assert_brand_free_capability_id",
    "is_semantic_capability",
]
