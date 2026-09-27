"""Prime launch settings precedence — Verdict's one-shot retry policy floor.

Verdict owns recovery: a Verdict-owned launch must make at most one model
request so the orchestrator classifies exactly one terminal per attempt
(ADR-036). Prime's own retry/wait/backup machinery is therefore disabled for
**Verdict-owned launches only**.

Sessions Verdict does not own (an operator's interactive ``prime-agent`` in this
checkout, an ad-hoc headless run) keep Prime's own retries. That is the owner
decision this module implements, and it is why the tracked project file
``.prime/agent/settings.json`` must not carry any retry key: the project layer
applies to *every* session whose cwd is this repo, so a retry key there would
decide policy for everyone.

Verified statically against the installed Prime 0.9.6 bundle:

* the agent dir is ``PRIME_AGENT_CODING_AGENT_DIR`` or ``~/.prime/agent``
  (``Zr()``), and it holds the whole global layer, not only settings:
  ``auth.json`` (credentials), ``models.json`` (provider/model registry),
  ``mcp-connections.json``, ``skills/``, ``extensions/``, ``harness/``,
  ``sessions/``, ``models/``, ``mcp/``, ``themes/``, ``logs/``, ``bin/``;
* global settings are ``<agentDir>/settings.json``; project settings are
  ``<cwd>/.prime/agent/settings.json`` with no upward search, where ``cwd`` is
  ``--cwd`` (Prime chdir's to it) or the process cwd;
* the two are merged by a one-level nested merge in which **project wins** per
  key. A nested object merges only one level deep, so a project
  ``retry.provider`` replaces the global ``retry.provider`` wholesale — which is
  why the tracked project file carries no ``retry`` object at all;
* absent keys fall back to Prime's built-in defaults, which retry: see
  ``PRIME_BUILTIN_RETRY_DEFAULTS``.

So a Verdict launch gets its one-shot policy from a per-launch agent dir, and
that dir is a faithful mirror of the operator's agent dir with exactly one
difference: retry/backup policy. Anything else would change worker behavior
(lost credentials, lost provider registry, lost skills/MCP) instead of only
retry. Secret- and lock-sensitive files are copied rather than symlinked,
because Prime resolves symlinks before writing ``auth.json`` and takes its
lockfile on the unresolved path, so a symlink would let a launch write and
lock-bypass the operator's real credential file.

The policy is enforced on the *effective merged* settings, and a launch whose
effective settings would still permit Prime-side retries is refused instead of
run. A hidden retry would spend quota and mis-attribute a terminal, which is
worse than a named, actionable failure.

Stdlib only: ``scripts/prime_supervisor.py`` path-loads this module without
importing the package root.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_AGENT_DIR_RELPATH",
    "LAUNCH_PRIVATE_COPIES",
    "PRIME_AGENT_DIR_ENV",
    "PRIME_BUILTIN_RETRY_DEFAULTS",
    "PROJECT_SETTINGS_RELPATH",
    "LaunchAgentDir",
    "PrimeRetryPolicyError",
    "assert_one_shot_launch",
    "build_launch_settings",
    "default_prime_agent_dir",
    "effective_prime_settings",
    "load_prime_settings_file",
    "merge_prime_settings",
    "one_shot_prime_settings",
    "prepare_launch_agent_dir",
    "prime_retry_policy_problems",
    "resolved_retry_policy",
]

#: Prime's project settings file, relative to the launch cwd (``piConfig.configDir``).
PROJECT_SETTINGS_RELPATH = Path(".prime/agent/settings.json")

#: The environment variable Prime 0.9.6 reads for its agent (global) dir.
PRIME_AGENT_DIR_ENV = "PRIME_AGENT_CODING_AGENT_DIR"

#: Prime's default agent dir, relative to the user's home.
DEFAULT_AGENT_DIR_RELPATH = Path(".prime/agent")

#: Agent-dir files copied into a per-launch dir instead of symlinked.
#:
#: Prime writes ``auth.json`` through ``realpath`` and locks it on the
#: unresolved path, so a symlink would both write the operator's real
#: credentials and bypass the lock a concurrent session holds. ``models.json``
#: and ``mcp-connections.json`` are rewritten in place by Prime for the same
#: kind of registry edit. Copies keep those writes inside the launch dir, where
#: they are discarded with it.
LAUNCH_PRIVATE_COPIES = ("auth.json", "models.json", "mcp-connections.json")

#: Prime 0.9.6's built-in retry defaults, from static inspection of the bundle
#: (``SettingsManager.getRetrySettings`` / ``getProviderRetrySettings`` /
#: ``getProviderWaitSettings``, and the ``{enabled:true,maxRetries:3,...}``
#: policy default). These are what a session with no ``retry`` key gets, so an
#: absent key is a retry-enabled launch, not a safe one.
PRIME_BUILTIN_RETRY_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "maxRetries": 3,
    "baseDelayMs": 2000,
    "maxRetryDelayMs": 60000,
    "waitForUsage": {"enabled": True, "pauseUntilReset": True, "maxAttempts": 30, "maxParks": 8},
}


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
                # Verdict times out its own worker, but keep Prime's per-request
                # ceiling the repo used before this policy moved per-launch.
                "timeoutMs": 900000,
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


def default_prime_agent_dir(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the agent dir the way Prime's ``Zr()`` does."""
    source = os.environ if env is None else env
    raw = (source.get(PRIME_AGENT_DIR_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_AGENT_DIR_RELPATH


def build_launch_settings(global_settings: Mapping[str, Any]) -> dict[str, Any]:
    """The operator's global settings with only the retry/backup policy replaced.

    The ``retry`` block is replaced wholesale rather than merged, so no operator
    sub-key (a lingering ``maxRetries``, a ``waitForUsage`` window) survives
    into a Verdict launch. Everything else — ``mcpServers``, ``enabledModels``,
    ``skills``, ``packages``, ``compaction`` — is kept, because a Verdict launch
    must differ from a manual one in retry policy only.
    """
    merged = dict(global_settings)
    merged.update(one_shot_prime_settings())
    return merged


@dataclass(frozen=True)
class LaunchAgentDir:
    """A per-launch agent dir: the operator's, minus Prime's retry policy."""

    path: Path
    source: Path | None
    copied: tuple[str, ...]
    linked: tuple[str, ...]

    @property
    def settings_path(self) -> Path:
        return self.path / "settings.json"


def prepare_launch_agent_dir(path: Path, *, source: Path | None = None) -> LaunchAgentDir:
    """Build a per-launch agent dir for ``PRIME_AGENT_CODING_AGENT_DIR``.

    ``PRIME_AGENT_CODING_AGENT_DIR`` replaces the whole agent dir, not just
    settings, so an empty dir would strip a real launch of credentials
    (``auth.json``), the provider/model registry (``models.json``), MCP
    connections, skills, extensions and harness state. Every entry of ``source``
    is therefore mirrored; only ``settings.json`` is authored here.

    Entries in :data:`LAUNCH_PRIVATE_COPIES` are copied (mode ``0600``) so Prime
    writes and lockfiles stay inside the launch dir. Everything else is
    symlinked, which keeps the operator's own storage (sessions, logs, harness)
    exactly where a manual session puts it.
    """
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    os.chmod(target, 0o700)
    origin = default_prime_agent_dir() if source is None else Path(source).expanduser()
    copied: list[str] = []
    linked: list[str] = []
    try:
        entries = sorted(os.listdir(origin))
    except OSError:
        entries = []
    for name in entries:
        if name == "settings.json":  # authored below, never the operator's file
            continue
        origin_entry = origin / name
        destination = target / name
        if destination.exists() or destination.is_symlink():
            continue
        if name in LAUNCH_PRIVATE_COPIES:
            if origin_entry.is_file():
                shutil.copyfile(origin_entry, destination)
                os.chmod(destination, 0o600)
                copied.append(name)
            continue
        destination.symlink_to(origin_entry)
        linked.append(name)
    settings = build_launch_settings(load_prime_settings_file(origin / "settings.json"))
    settings_path = target / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.chmod(settings_path, 0o600)
    return LaunchAgentDir(
        path=target,
        source=origin if origin.is_dir() else None,
        copied=tuple(copied),
        linked=tuple(linked),
    )


def _is_off(container: Mapping[str, Any], key: str) -> bool:
    """True when ``key`` is explicitly false. Prime defaults every switch to on."""
    return container.get(key, True) is False


def _mapping(container: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = container.get(key)
    return value if isinstance(value, Mapping) else {}


def resolved_retry_policy(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Apply Prime 0.9.6's built-in defaults to ``settings``.

    This is what a session actually gets, so it answers both halves of the owner
    decision: a Verdict launch resolves to retries off, a manual session with no
    ``retry`` key resolves to :data:`PRIME_BUILTIN_RETRY_DEFAULTS`.
    """
    defaults = PRIME_BUILTIN_RETRY_DEFAULTS
    wait_defaults = dict(defaults["waitForUsage"])
    retry = _mapping(settings, "retry")
    provider = _mapping(retry, "provider")
    wait = _mapping(provider, "waitForUsage")
    return {
        "enabled": retry.get("enabled", defaults["enabled"]),
        "maxRetries": retry.get("maxRetries", defaults["maxRetries"]),
        "baseDelayMs": retry.get("baseDelayMs", defaults["baseDelayMs"]),
        "maxRetryDelayMs": provider.get("maxRetryDelayMs", defaults["maxRetryDelayMs"]),
        "waitForUsage": {
            "enabled": wait.get("enabled", wait_defaults["enabled"]),
            "pauseUntilReset": wait.get("pauseUntilReset", wait_defaults["pauseUntilReset"]),
            "maxAttempts": wait.get("maxAttempts", wait_defaults["maxAttempts"]),
            "maxParks": wait.get("maxParks", wait_defaults["maxParks"]),
        },
        "providerBackupModel": str(settings.get("providerBackupModel", "") or "").strip(),
    }


def prime_retry_policy_problems(settings: Mapping[str, Any]) -> list[str]:
    """Name every way ``settings`` would still let Prime retry, wait, or fail over.

    Defaults mirror Prime 0.9.6 (see :data:`PRIME_BUILTIN_RETRY_DEFAULTS`), so
    an absent key is a problem, not a pass.
    """
    problems: list[str] = []
    retry = _mapping(settings, "retry")
    if not _is_off(retry, "enabled"):
        problems.append("retry.enabled must be false")
    if retry.get("maxRetries", PRIME_BUILTIN_RETRY_DEFAULTS["maxRetries"]) != 0:
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
