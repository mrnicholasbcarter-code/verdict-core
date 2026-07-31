"""Read-only setup planning contracts.

The planner deliberately performs no discovery probes and never reads config
contents. It describes the first-run mutations that a future apply command may
request, leaving consent and execution to a separate transaction boundary.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class SetupAction:
    """One proposed setup action, without executing it."""

    action_id: str
    kind: str
    target: str
    description: str
    reversible: bool
    requires_consent: bool

    def to_dict(self) -> dict[str, object]:
        """Return the stable JSON representation of the action."""

        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "target": self.target,
            "description": self.description,
            "reversible": self.reversible,
            "requires_consent": self.requires_consent,
        }


@dataclass(frozen=True)
class SetupPlan:
    """Stable, mutation-free setup plan returned by the CLI."""

    config_path: str
    config_exists: bool
    actions: tuple[SetupAction, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible plan with explicit safety status."""

        return {
            "kind": "setup_plan",
            "schema_version": SCHEMA_VERSION,
            "status": "planned",
            "mode": "dry-run",
            "mutation_free": True,
            "network_access": "disabled",
            "credential_access": "disabled",
            "config": {"path": self.config_path, "exists": self.config_exists},
            "actions": [action.to_dict() for action in self.actions],
            "next": "review this plan before a future setup apply command",
        }


def _display_config_path() -> str:
    """Return a stable, non-machine-specific config path display."""

    if os.environ.get("XDG_CONFIG_HOME"):
        return "$XDG_CONFIG_HOME/verdict/verdict.yaml"
    return "~/.config/verdict/verdict.yaml"


def _config_path() -> Path:
    """Resolve the current CLI config path without reading its contents."""

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "verdict" / "verdict.yaml"
    return Path.home() / ".config" / "verdict" / "verdict.yaml"


def build_setup_plan() -> SetupPlan:
    """Build a deterministic local setup plan without network or mutations."""

    config_exists = _config_path().is_file()
    actions = (
        (
            SetupAction(
                action_id="preserve-config",
                kind="inspect",
                target="config",
                description="Preserve the existing Verdict configuration unchanged.",
                reversible=True,
                requires_consent=False,
            ),
        )
        if config_exists
        else (
            SetupAction(
                action_id="create-config",
                kind="write_config",
                target="config",
                description="Create a minimal Verdict configuration after explicit consent.",
                reversible=True,
                requires_consent=True,
            ),
        )
    )
    return SetupPlan(
        config_path=_display_config_path(), config_exists=config_exists, actions=actions
    )


__all__ = ["SCHEMA_VERSION", "SetupAction", "SetupPlan", "build_setup_plan"]
