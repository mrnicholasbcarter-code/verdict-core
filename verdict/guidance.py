"""Optional, platform-neutral project guidance boundary.

The guidance boundary is deliberately independent of Codex, Claude Code, Pi,
Ruflo, and provider CLIs.  It loads a bounded Markdown policy document only
when explicitly enabled and never treats guidance as an authorization grant.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any, Literal, cast

GuidanceState = Literal["disabled", "ready", "degraded"]
GuidanceDecision = Literal["allow", "approval_required", "deny"]

DEFAULT_GUIDANCE_FILENAME = "GUIDANCE.md"
DEFAULT_INIT_TIMEOUT_MS = 1000
DEFAULT_MAX_BYTES = 128 * 1024
DEFAULT_MAX_RULES = 1000


@dataclass(frozen=True)
class GuidanceConfig:
    """Configuration for the optional guidance boundary."""

    enabled: bool
    repo_root: Path
    guidance_path: Path
    local_path: Path | None = None
    init_timeout_ms: int = DEFAULT_INIT_TIMEOUT_MS
    max_bytes: int = DEFAULT_MAX_BYTES
    max_rules: int = DEFAULT_MAX_RULES

    @classmethod
    def from_environment(cls, repo_root: Path | None = None) -> GuidanceConfig:
        """Build configuration without reading guidance or host-specific state."""

        root = (repo_root or Path.cwd()).resolve()
        enabled = _env_flag("VERDICT_GUIDANCE_ENABLED")
        if not enabled:
            return cls(
                enabled=False, repo_root=root, guidance_path=root / DEFAULT_GUIDANCE_FILENAME
            )
        guidance_path = _configured_path(
            root, os.getenv("VERDICT_GUIDANCE_PATH", DEFAULT_GUIDANCE_FILENAME)
        )
        local_value = os.getenv("VERDICT_GUIDANCE_LOCAL_PATH")
        local_path = _configured_path(root, local_value) if local_value else None
        return cls(
            enabled=enabled,
            repo_root=root,
            guidance_path=guidance_path,
            local_path=local_path,
            init_timeout_ms=_positive_env_int(
                "VERDICT_GUIDANCE_INIT_TIMEOUT_MS", DEFAULT_INIT_TIMEOUT_MS
            ),
            max_bytes=_positive_env_int("VERDICT_GUIDANCE_MAX_BYTES", DEFAULT_MAX_BYTES),
            max_rules=_positive_env_int("VERDICT_GUIDANCE_MAX_RULES", DEFAULT_MAX_RULES),
        )


@dataclass(frozen=True)
class GuidanceRule:
    """A source-attributed rule extracted from a guidance document."""

    rule_id: str
    content: str
    source: str
    decision: GuidanceDecision | None = None


@dataclass(frozen=True)
class GuidanceStatus:
    """Observable initialization state for the optional boundary."""

    state: GuidanceState
    enabled: bool
    reason: str | None = None
    policy_version: str | None = None
    loaded_at: float | None = None
    initialization_ms: float | None = None


class GuidanceControlPlane:
    """Bounded evaluator for platform-neutral project guidance."""

    def __init__(self, config: GuidanceConfig, status: GuidanceStatus) -> None:
        self.config = config
        self.status = status
        self._rules: tuple[GuidanceRule, ...] = ()

    @classmethod
    def disabled(cls, config: GuidanceConfig) -> GuidanceControlPlane:
        """Create the inert default-off boundary without reading guidance."""

        return cls(
            config, GuidanceStatus(state="disabled", enabled=False, reason="feature_disabled")
        )

    @classmethod
    def degraded(cls, config: GuidanceConfig, reason: str) -> GuidanceControlPlane:
        """Create an explicitly enabled, safely degraded control plane."""

        return cls(config, GuidanceStatus(state="degraded", enabled=True, reason=reason))

    @classmethod
    async def initialize(cls, config: GuidanceConfig) -> GuidanceControlPlane:
        """Initialize without blocking startup beyond the configured timeout."""

        started = monotonic()
        if not config.enabled:
            return cls.disabled(config)

        try:
            rules, policy_version = await asyncio.wait_for(
                asyncio.to_thread(_load_rules, config), timeout=config.init_timeout_ms / 1000
            )
        except asyncio.TimeoutError:
            return cls(
                config,
                GuidanceStatus(
                    state="degraded",
                    enabled=True,
                    reason="initialization_timeout",
                    initialization_ms=(monotonic() - started) * 1000,
                ),
            )
        except (GuidanceConfigurationError, UnicodeDecodeError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, GuidanceConfigurationError)
                else "guidance_file_invalid_utf8"
            )
            return cls(
                config,
                GuidanceStatus(
                    state="degraded",
                    enabled=True,
                    reason=reason,
                    initialization_ms=(monotonic() - started) * 1000,
                ),
            )
        except OSError as exc:
            return cls(
                config,
                GuidanceStatus(
                    state="degraded",
                    enabled=True,
                    reason=f"guidance_read_failed:{type(exc).__name__}",
                    initialization_ms=(monotonic() - started) * 1000,
                ),
            )

        instance = cls(
            config,
            GuidanceStatus(
                state="ready",
                enabled=True,
                policy_version=policy_version,
                loaded_at=monotonic(),
                initialization_ms=(monotonic() - started) * 1000,
            ),
        )
        instance._rules = tuple(rules)
        return instance

    def evaluate(self, task: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a normalized task without granting execution authority."""

        if self.status.state != "ready":
            raise GuidanceUnavailableError(self.status.reason or "guidance_unavailable")

        goal = task["goal"]
        keywords = _keywords(goal)
        matched = [rule for rule in self._rules if keywords.intersection(_keywords(rule.content))]
        decisions = {rule.decision for rule in matched if rule.decision is not None}
        if "deny" in decisions:
            decision: GuidanceDecision = "deny"
        elif "approval_required" in decisions or task.get("protected_work", False):
            decision = "approval_required"
        else:
            decision = "allow"

        return {
            "schema_version": "1",
            "decision": decision,
            "authorization": "unchanged",
            "task": task,
            "matched_rules": [
                {"rule_id": rule.rule_id, "source": rule.source, "content": rule.content}
                for rule in matched[:20]
            ],
            "policy_version": self.status.policy_version,
        }


class GuidanceConfigurationError(ValueError):
    """Raised when explicitly configured guidance cannot be safely loaded."""


class GuidanceUnavailableError(RuntimeError):
    """Raised when guidance was explicitly enabled but is not ready."""


def _env_flag(name: str) -> bool:
    return os.getenv(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def _positive_env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise GuidanceConfigurationError(f"invalid_{name.lower()}") from exc
    if parsed <= 0:
        raise GuidanceConfigurationError(f"invalid_{name.lower()}")
    return parsed


def _configured_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise GuidanceConfigurationError("guidance_path_outside_repo_root") from exc
    return resolved


def _load_rules(config: GuidanceConfig) -> tuple[list[GuidanceRule], str]:
    contents: list[tuple[str, str]] = []
    for path, source in ((config.guidance_path, "guidance"), (config.local_path, "local")):
        if path is None:
            continue
        if not path.exists():
            if source == "guidance":
                raise GuidanceConfigurationError("guidance_file_missing")
            continue
        if not path.is_file():
            raise GuidanceConfigurationError(f"guidance_path_not_file:{source}")
        data = path.read_bytes()
        if len(data) > config.max_bytes:
            raise GuidanceConfigurationError(f"guidance_file_too_large:{source}")
        contents.append((source, data.decode("utf-8")))

    combined = "\n".join(content for _, content in contents)
    rules: list[GuidanceRule] = []
    for source, content in contents:
        for line in content.splitlines():
            stripped = line.strip()
            if not (stripped.startswith("- ") or stripped.startswith("* ")):
                continue
            rule_content = stripped[2:].strip()
            decision: GuidanceDecision | None = None
            marker = re.match(r"^\[(deny|approval|allow)\]\s*", rule_content, re.IGNORECASE)
            if marker:
                marker_value = marker.group(1).lower()
                decision = cast(
                    GuidanceDecision,
                    {"deny": "deny", "approval": "approval_required", "allow": "allow"}[
                        marker_value
                    ],
                )
                rule_content = rule_content[marker.end() :].strip()
            digest = hashlib.sha256(f"{source}:{rule_content}".encode()).hexdigest()[:16]
            rules.append(GuidanceRule(digest, rule_content, source, decision))
            if len(rules) >= config.max_rules:
                break
        if len(rules) >= config.max_rules:
            break

    return rules, hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def _keywords(value: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[a-zA-Z0-9_]{3,}", value)}


__all__ = [
    "GuidanceConfig",
    "GuidanceConfigurationError",
    "GuidanceControlPlane",
    "GuidanceDecision",
    "GuidanceRule",
    "GuidanceState",
    "GuidanceStatus",
    "GuidanceUnavailableError",
]
