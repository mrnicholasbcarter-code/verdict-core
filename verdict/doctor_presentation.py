"""Repair command labels for existing doctor findings; never runs a repair.

Diagnosis remains in doctor_diagnostics. Unknown findings lead to a mutation-free
setup review, rather than a guessed provider install or a claimed repair.
"""

from __future__ import annotations

import re

PROBLEM_STATES = frozenset(
    {
        "failed",
        "error",
        "blocked",
        "missing",
        "partial",
        "degraded",
        "warn",
        "warning",
        "unhealthy",
        "unreachable",
        "unknown",
        "auth_failed",
        "not_installed",
        "uncovered",
    }
)


def repair_command(finding: str) -> str:
    """Return a safe, registered next-step command for a reported finding."""
    lowered = finding.lower()
    credential = re.search(r"\b([A-Z][A-Z0-9_]*(?:API_KEY|BASE_URL|AUTH_TOKEN))\b", finding)
    if credential and any(
        word in lowered
        for word in ("missing", "invalid", "not set", "auth", "credential", "format")
    ):
        return f"verdict credentials set {credential.group(1)}"
    if any(word in lowered for word in ("gateway", "omniroute", "host url")):
        return "verdict detect"
    if any(
        word in lowered
        for word in (
            "schema_version",
            "older verdict",
            "config.yaml",
            "missing_memory_db",
            "missing_mcp_config",
        )
    ):
        return "verdict doctor --fix"
    if any(word in lowered for word in ("configuration", "verdict.yaml", "primary model", "yaml")):
        return "verdict setup"
    if "credential" in lowered or "api key" in lowered or "auth" in lowered:
        return "verdict setup credentials"
    if "duplicate node" in lowered:
        return "verdict doctor --fix"
    return "verdict setup --recommended"
