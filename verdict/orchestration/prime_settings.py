"""Prime launch settings precedence — Verdict's one-shot retry policy floor.

Verdict owns recovery: one launch must make at most one model request so the
orchestrator classifies exactly one terminal per attempt (ADR-036). Prime's own
retry/wait/backup machinery is therefore disabled per launch.

Disabling it needs Prime's real precedence, not just an isolated config dir.
Verified statically against the installed Prime 0.9.6 bundle:

* global settings come from ``<agentDir>/settings.json`` where ``agentDir`` is
  ``PRIME_AGENT_CODING_AGENT_DIR`` or ``~/.prime/agent``;
* project settings come from ``<cwd>/.prime/agent/settings.json`` with no
  upward search, where ``cwd`` is ``--cwd`` (Prime chdir's to it) or the
  process cwd;
* the two are merged by a one-level nested merge in which **project wins**
  per key. A nested object merges only one level deep, so a project
  ``retry.provider`` replaces the global ``retry.provider`` wholesale.

Prime 0.9.6 exposes **no** per-launch escape from that order: no CLI flag, no
environment variable, no daemon/session-config field, and
``SettingsManager.applyOverrides`` has no call site in the CLI bundle (it is an
SDK-only surface). ``PRIME_AGENT_CODING_AGENT_DIR`` replaces only the *global*
directory, so a project file that re-enables retry still wins.

So the one-shot policy is enforced on the *effective merged* settings, and a
launch whose effective settings would still permit Prime-side retries is
refused instead of run. A hidden retry would spend quota and mis-attribute a
terminal, which is worse than a named, actionable failure.

Stdlib only: ``scripts/prime_supervisor.py`` path-loads this module without
importing the package root.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "PROJECT_SETTINGS_RELPATH",
    "PrimeRetryPolicyError",
    "assert_one_shot_launch",
    "effective_prime_settings",
    "load_prime_settings_file",
    "merge_prime_settings",
    "one_shot_prime_settings",
    "prime_retry_policy_problems",
]

#: Prime's project settings file, relative to the launch cwd (``piConfig.configDir``).
PROJECT_SETTINGS_RELPATH = Path(".prime/agent/settings.json")


class PrimeRetryPolicyError(RuntimeError):
    """Raised instead of launching when Prime would keep its own retries on."""


def one_shot_prime_settings() -> dict[str, Any]:
    """Settings that make a Prime launch make at most one model request."""
    return {
        "retry": {
            "enabled": False,
            "maxRetries": 0,
            "baseDelayMs": 0,
            "provider": {
                "maxRetryDelayMs": 0,
                "waitForUsage": {"enabled": False, "pauseUntilReset": False},
            },
        },
        "providerBackupModel": "",
    }


def merge_prime_settings(
    global_settings: Mapping[str, Any], project_settings: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge like Prime 0.9.6: project wins, nested objects merge one level deep.

    JSON cannot express JavaScript ``undefined``, so every project key present
    in the file participates in the merge (Prime skips only ``undefined``).
    """
    merged: dict[str, Any] = dict(global_settings)
    for key, value in project_settings.items():
        base = global_settings.get(key)
        if (
            isinstance(value, Mapping)
            and not isinstance(value, (str, bytes))
            and isinstance(base, Mapping)
            and not isinstance(base, (str, bytes))
        ):
            merged[key] = {**base, **value}
        else:
            merged[key] = value
    return merged


def load_prime_settings_file(path: Path) -> dict[str, Any]:
    """Read one settings file. Missing or unreadable content reads as ``{}``.

    Prime treats a settings file that fails to parse as empty for that scope
    (it records the error and keeps running), so this does the same rather than
    inventing a stricter contract than the binary's.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def effective_prime_settings(*, cwd: Path, config_dir: Path) -> dict[str, Any]:
    """The settings a real Prime launch would apply for ``cwd``/``config_dir``."""
    return merge_prime_settings(
        load_prime_settings_file(Path(config_dir) / "settings.json"),
        load_prime_settings_file(Path(cwd) / PROJECT_SETTINGS_RELPATH),
    )


def _is_off(container: Mapping[str, Any], key: str) -> bool:
    """True when ``key`` is explicitly false. Prime defaults every switch to on."""
    return container.get(key, True) is False


def _mapping(container: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = container.get(key)
    return value if isinstance(value, Mapping) else {}


def prime_retry_policy_problems(settings: Mapping[str, Any]) -> list[str]:
    """Name every way ``settings`` would still let Prime retry, wait, or fail over.

    Defaults mirror Prime 0.9.6: ``retry.enabled`` true, ``retry.maxRetries``
    3, ``waitForUsage.enabled`` true, ``pauseUntilReset`` true. An absent key
    is therefore a problem, not a pass.
    """
    problems: list[str] = []
    retry = _mapping(settings, "retry")
    if not _is_off(retry, "enabled"):
        problems.append("retry.enabled must be false")
    if retry.get("maxRetries", 3) != 0:
        problems.append("retry.maxRetries must be 0")
    wait = _mapping(_mapping(retry, "provider"), "waitForUsage")
    if not _is_off(wait, "enabled"):
        problems.append("retry.provider.waitForUsage.enabled must be false")
    if not _is_off(wait, "pauseUntilReset"):
        problems.append("retry.provider.waitForUsage.pauseUntilReset must be false")
    if str(settings.get("providerBackupModel", "") or "").strip():
        problems.append("providerBackupModel must be empty")
    return problems


def assert_one_shot_launch(*, cwd: Path, config_dir: Path) -> dict[str, Any]:
    """Return the effective settings, or raise when they are not one-shot.

    The message names the project file, because that is the only scope a
    per-launch config dir cannot override in Prime 0.9.6.
    """
    effective = effective_prime_settings(cwd=cwd, config_dir=config_dir)
    problems = prime_retry_policy_problems(effective)
    if problems:
        raise PrimeRetryPolicyError(
            "Prime would keep its own retry policy for this launch: "
            + "; ".join(problems)
            + f". Project settings win over the per-launch config dir in Prime, so fix "
            f"{Path(cwd) / PROJECT_SETTINGS_RELPATH}."
        )
    return effective
