"""Shared hook-command normalization used by the Claude and Codex installers."""

from __future__ import annotations

import re


def _normalize_hook_command(cmd: str) -> str:
    """Normalize hook command for deduplication."""
    # Remove extra spaces, normalize quotes
    cmd = re.sub(r"\s+", " ", cmd.strip())
    # Remove the 2>/dev/null || true suffix for comparison
    cmd = re.sub(r"\s*2>/dev/null\s*\|\|\s*true\s*$", "", cmd)
    # Normalize quote styles
    cmd = cmd.replace('"', "'").replace("`", "'")
    return cmd
