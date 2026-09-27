"""Prime launch settings precedence: the EFFECTIVE one-shot retry policy floor.

These tests model Prime 0.9.6's real merge (``global`` then ``project``, project
wins, nested objects merge one level deep) and assert the settings a launch
would actually apply — not just the contents of Verdict's per-launch agent dir.

The owner rule these tests encode: retries are off for **Verdict-owned launches
only**. A Verdict launch carries its one-shot policy in a per-launch agent dir
(``PRIME_AGENT_CODING_AGENT_DIR``); a manual session in this checkout has no
such override and keeps Prime's built-in retry defaults. The tracked project
file therefore must set no retry key at all, since the project layer applies to
every session whose cwd is this repo. Prime 0.9.6 still has no per-launch escape
from project precedence, so a project file that *does* enable retry makes a
Verdict launch refuse to run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.orchestration.prime_settings import (
    LAUNCH_PRIVATE_COPIES,
    PRIME_BUILTIN_RETRY_DEFAULTS,
    PROJECT_SETTINGS_RELPATH,
    LaunchAgentDir,
    PrimeRetryPolicyError,
    assert_one_shot_launch,
    build_launch_settings,
    effective_prime_settings,
    merge_prime_settings,
    one_shot_prime_settings,
    prepare_launch_agent_dir,
    prime_retry_policy_problems,
    resolved_retry_policy,
)

ROOT = Path(__file__).resolve().parent.parent
TRACKED_PROJECT_SETTINGS = ROOT / PROJECT_SETTINGS_RELPATH


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _launch_dirs(tmp_path: Path, project: object | None) -> tuple[Path, Path]:
    """A launch cwd (optionally with project settings) and a one-shot config dir."""
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    if project is not None:
        _write(cwd / PROJECT_SETTINGS_RELPATH, project)
    config_dir = tmp_path / "launch-config"
    config_dir.mkdir()
    _write(config_dir / "settings.json", one_shot_prime_settings())
    return cwd, config_dir


# ------------------------------------------------------- Prime's merge semantics


def test_project_settings_win_over_global_like_prime_does() -> None:
    merged = merge_prime_settings({"a": 1, "b": 2}, {"b": 3})
    assert merged == {"a": 1, "b": 3}


def test_nested_objects_merge_exactly_one_level_deep() -> None:
    """Prime's merge is ``{...global[key], ...project[key]}`` for objects.

    So a nested grandchild object in the project value REPLACES the global
    grandchild rather than merging into it.
    """
    merged = merge_prime_settings(
        {"retry": {"enabled": False, "provider": {"maxRetryDelayMs": 0, "timeoutMs": 1}}},
        {"retry": {"provider": {"timeoutMs": 2}}},
    )
    assert merged["retry"]["enabled"] is False  # untouched global key survives
    assert merged["retry"]["provider"] == {"timeoutMs": 2}  # grandchild replaced whole


def test_unreadable_or_invalid_settings_read_as_empty(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert effective_prime_settings(cwd=tmp_path, config_dir=tmp_path) == {}


# -------------------------------------------------------------- the policy floor


def test_one_shot_settings_satisfy_the_policy() -> None:
    assert prime_retry_policy_problems(one_shot_prime_settings()) == []


def test_empty_settings_are_a_problem_because_prime_defaults_to_retrying() -> None:
    problems = prime_retry_policy_problems({})
    assert "retry.enabled must be false" in problems
    assert "retry.maxRetries must be 0" in problems
    assert "retry.provider.waitForUsage.enabled must be false" in problems
    assert "retry.provider.waitForUsage.pauseUntilReset must be false" in problems


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"retry": {"enabled": True}}, "retry.enabled must be false"),
        ({"retry": {"maxRetries": 3}}, "retry.maxRetries must be 0"),
        (
            {"retry": {"provider": {"waitForUsage": {"enabled": True}}}},
            "retry.provider.waitForUsage.enabled must be false",
        ),
        (
            {"retry": {"provider": {"waitForUsage": {"pauseUntilReset": True}}}},
            "retry.provider.waitForUsage.pauseUntilReset must be false",
        ),
        ({"providerBackupModel": "anthropic/some-model"}, "providerBackupModel must be empty"),
    ],
)
def test_each_retry_layer_is_caught_in_the_effective_merge(
    tmp_path: Path, mutation: dict[str, object], expected: str
) -> None:
    """A conflicting PROJECT file must be caught, since project wins in Prime."""
    cwd, config_dir = _launch_dirs(tmp_path, mutation)
    problems = prime_retry_policy_problems(effective_prime_settings(cwd=cwd, config_dir=config_dir))
    assert expected in problems
    with pytest.raises(PrimeRetryPolicyError) as raised:
        assert_one_shot_launch(cwd=cwd, config_dir=config_dir)
    assert expected in str(raised.value)
    assert str(cwd / PROJECT_SETTINGS_RELPATH) in str(raised.value)


def test_isolated_config_dir_alone_does_not_satisfy_the_policy(tmp_path: Path) -> None:
    """The exact BOD266-2 defect: one-shot global + retrying project still retries."""
    cwd, config_dir = _launch_dirs(tmp_path, {"retry": {"enabled": True, "maxRetries": 3}})
    assert json.loads((config_dir / "settings.json").read_text()) == one_shot_prime_settings()
    effective = effective_prime_settings(cwd=cwd, config_dir=config_dir)
    assert effective["retry"]["enabled"] is True
    assert effective["retry"]["maxRetries"] == 3
    assert prime_retry_policy_problems(effective) != []


def test_launch_with_no_project_settings_is_one_shot(tmp_path: Path) -> None:
    cwd, config_dir = _launch_dirs(tmp_path, None)
    assert assert_one_shot_launch(cwd=cwd, config_dir=config_dir) == one_shot_prime_settings()


# ------------------------------------------------- the tracked project settings


def test_tracked_project_settings_set_no_retry_policy() -> None:
    """The tracked project file must not decide retry policy for anyone.

    This replaces the older rule ("the tracked file itself disables retries").
    That rule was correct about Prime precedence but wrong about scope: the
    project layer applies to EVERY session whose cwd is this repo, including an
    operator's interactive session, so disabling retry there took Prime's own
    recovery away from sessions Verdict does not own. Retries are now off for
    Verdict-owned launches only, and those carry the policy in a per-launch
    agent dir, so the tracked file must stay silent on retry.
    """
    tracked = json.loads(TRACKED_PROJECT_SETTINGS.read_text(encoding="utf-8"))
    assert "retry" not in tracked, "tracked project settings must not set a retry policy"
    flat = json.dumps(tracked)
    for key in ("enabled", "maxRetries", "baseDelayMs", "waitForUsage", "pauseUntilReset"):
        assert f'"{key}"' not in flat or key not in json.dumps(tracked.get("retry", {}))
    # An empty providerBackupModel is a no-op that matches Prime's own default
    # (no backup model), so it takes nothing away from a manual session.
    assert tracked.get("providerBackupModel", "") == ""
    # Unrelated keys survive: the file still carries the repo's compaction policy.
    assert tracked["compaction"]["enabled"] is True


def test_tracked_project_settings_keep_retry_provider_out_of_the_merge() -> None:
    """Prime merges nested objects one level deep, so a project ``retry.provider``
    would REPLACE the per-launch ``retry.provider`` wholesale and silently drop
    Verdict's wait/park suppression. The tracked file therefore keeps no
    ``retry`` object at all, not even a provider-only one."""
    tracked = json.loads(TRACKED_PROJECT_SETTINGS.read_text(encoding="utf-8"))
    launch = one_shot_prime_settings()
    merged = merge_prime_settings(launch, tracked)
    assert merged["retry"] == launch["retry"]
    assert prime_retry_policy_problems(merged) == []


def test_verdict_launch_in_this_repo_has_retries_off(tmp_path: Path) -> None:
    """A Verdict launch: per-launch agent dir + the tracked project file."""
    launch = prepare_launch_agent_dir(tmp_path / "launch", source=tmp_path / "absent-global")
    effective = effective_prime_settings(cwd=ROOT, config_dir=launch.path)
    assert effective["retry"]["enabled"] is False
    assert effective["retry"]["maxRetries"] == 0
    wait = effective["retry"]["provider"]["waitForUsage"]
    assert wait["enabled"] is False
    assert wait["pauseUntilReset"] is False
    assert effective["providerBackupModel"] == ""
    assert assert_one_shot_launch(cwd=ROOT, config_dir=launch.path) == effective
    assert resolved_retry_policy(effective)["enabled"] is False
    assert resolved_retry_policy(effective)["maxRetries"] == 0


def test_manual_launch_in_this_repo_keeps_prime_retries(tmp_path: Path) -> None:
    """A session Verdict does not own: no per-launch override, and a global
    settings file with no ``retry`` key (the operator's real shape). It must keep
    Prime's built-in retry defaults, which is the owner decision."""
    global_dir = tmp_path / "operator-agent-dir"
    global_dir.mkdir()
    _write(
        global_dir / "settings.json",
        {"defaultProvider": "omniroute", "theme": "dark"},  # no retry key
    )
    effective = effective_prime_settings(cwd=ROOT, config_dir=global_dir)
    assert "retry" not in effective
    policy = resolved_retry_policy(effective)
    assert policy["enabled"] is True
    assert policy["maxRetries"] == PRIME_BUILTIN_RETRY_DEFAULTS["maxRetries"] == 3
    assert policy["baseDelayMs"] == PRIME_BUILTIN_RETRY_DEFAULTS["baseDelayMs"] == 2000
    assert policy["waitForUsage"]["enabled"] is True
    assert policy["waitForUsage"]["pauseUntilReset"] is True
    # The same settings are NOT one-shot, which is exactly why Verdict needs its
    # own per-launch dir instead of relying on the project file.
    assert prime_retry_policy_problems(effective) != []


def test_manual_launch_keeps_retries_even_though_tracked_file_is_present() -> None:
    """The tracked file is in the merge for a manual session too, so this is the
    regression that fails if a retry key is ever put back into it."""
    tracked = json.loads(TRACKED_PROJECT_SETTINGS.read_text(encoding="utf-8"))
    effective = merge_prime_settings({}, tracked)
    policy = resolved_retry_policy(effective)
    assert policy["enabled"] is True
    assert policy["maxRetries"] == 3
    assert policy["waitForUsage"]["enabled"] is True


# --------------------------------------------------- the per-launch agent dir


def test_launch_agent_dir_exposes_the_files_prime_reads(tmp_path: Path) -> None:
    """``PRIME_AGENT_CODING_AGENT_DIR`` replaces the WHOLE agent dir, not just
    settings. Prime resolves ``auth.json`` (credentials), ``models.json``
    (provider/model registry), ``mcp-connections.json``, ``skills/`` and
    ``extensions/`` from it, so a bare dir would launch a worker with no
    credentials and no models. Every entry must therefore be reachable."""
    origin = tmp_path / "agent"
    (origin / "skills" / "demo").mkdir(parents=True)
    (origin / "extensions").mkdir()
    _write(origin / "settings.json", {"theme": "dark", "retry": {"enabled": True}})
    _write(origin / "auth.json", {"omniroute": {"type": "api_key", "key": "placeholder"}})
    _write(origin / "models.json", {"providers": {"omniroute": {"models": ["a"]}}})
    _write(origin / "mcp-connections.json", {"records": []})

    launch = prepare_launch_agent_dir(tmp_path / "launch", source=origin)

    assert isinstance(launch, LaunchAgentDir)
    for name in ("auth.json", "models.json", "mcp-connections.json", "skills", "extensions"):
        assert (launch.path / name).exists(), f"launch dir lost {name}"
    assert json.loads((launch.path / "auth.json").read_text()) == json.loads(
        (origin / "auth.json").read_text()
    )
    assert json.loads((launch.path / "models.json").read_text())["providers"]["omniroute"] == {
        "models": ["a"]
    }
    assert (launch.path / "skills" / "demo").is_dir()
    assert set(launch.copied) == set(LAUNCH_PRIVATE_COPIES)
    assert "skills" in launch.linked and "extensions" in launch.linked


def test_launch_agent_dir_copies_secret_and_lock_sensitive_files(tmp_path: Path) -> None:
    """Prime writes ``auth.json`` through ``realpath`` and locks the unresolved
    path, so a symlink would let a launch write the operator's real credentials
    and bypass a concurrent session's lock. Those files are copied, mode 0600,
    and writing the copy must not touch the original."""
    origin = tmp_path / "agent"
    origin.mkdir()
    _write(origin / "auth.json", {"omniroute": {"type": "api_key", "key": "placeholder"}})
    _write(origin / "models.json", {"providers": {}})
    before = (origin / "auth.json").read_text()

    launch = prepare_launch_agent_dir(tmp_path / "launch", source=origin)

    for name in ("auth.json", "models.json"):
        assert not (launch.path / name).is_symlink(), f"{name} must be a copy, not a symlink"
        assert (launch.path / name).stat().st_mode & 0o777 == 0o600
    (launch.path / "auth.json").write_text(json.dumps({"other": "written-by-launch"}))
    assert (origin / "auth.json").read_text() == before  # operator file untouched


def test_launch_settings_replace_only_the_retry_policy(tmp_path: Path) -> None:
    """A Verdict launch must differ from a manual one in retry policy only, so
    the operator's unrelated global keys are preserved and the whole ``retry``
    block is replaced (not merged) so no stale sub-key survives."""
    global_settings = {
        "defaultProvider": "omniroute",
        "mcpServers": {"linear": {"command": "x"}},
        "compaction": {"enabled": True},
        "retry": {"enabled": True, "maxRetries": 7, "provider": {"waitForUsage": {"maxParks": 4}}},
        "providerBackupModel": "vendor/backup",
    }
    built = build_launch_settings(global_settings)
    assert built["defaultProvider"] == "omniroute"
    assert built["mcpServers"] == {"linear": {"command": "x"}}
    assert built["compaction"] == {"enabled": True}
    assert built["retry"] == one_shot_prime_settings()["retry"]
    assert "maxParks" not in json.dumps(built["retry"])
    assert built["providerBackupModel"] == ""
    assert prime_retry_policy_problems(built) == []

    launch = prepare_launch_agent_dir(tmp_path / "launch", source=tmp_path / "missing")
    _write(tmp_path / "global" / "settings.json", global_settings)
    from_disk = prepare_launch_agent_dir(tmp_path / "launch2", source=tmp_path / "global")
    assert (
        prime_retry_policy_problems(json.loads(from_disk.settings_path.read_text(encoding="utf-8")))
        == []
    )
    assert (
        prime_retry_policy_problems(json.loads(launch.settings_path.read_text(encoding="utf-8")))
        == []
    )


def test_missing_global_agent_dir_still_yields_a_one_shot_launch(tmp_path: Path) -> None:
    """A launch must not depend on the operator's dir existing (CI, fresh host)."""
    launch = prepare_launch_agent_dir(tmp_path / "launch", source=tmp_path / "nope")
    assert launch.source is None
    assert launch.copied == ()
    assert launch.linked == ()
    assert (
        prime_retry_policy_problems(json.loads(launch.settings_path.read_text(encoding="utf-8")))
        == []
    )


# ------------------------------------------------ Prime 0.9.6 built-in defaults


def test_builtin_retry_defaults_match_the_inspected_bundle() -> None:
    """Static inspection of prime-agent 0.9.6: ``getRetrySettings`` defaults to
    ``enabled true / maxRetries 3 / baseDelayMs 2000``,
    ``getProviderRetrySettings`` to ``maxRetryDelayMs 60000``, and
    ``getProviderWaitSettings`` to ``enabled true / pauseUntilReset true /
    maxAttempts 30 / maxParks 8``. Manual sessions now get these."""
    assert PRIME_BUILTIN_RETRY_DEFAULTS == {
        "enabled": True,
        "maxRetries": 3,
        "baseDelayMs": 2000,
        "maxRetryDelayMs": 60000,
        "waitForUsage": {
            "enabled": True,
            "pauseUntilReset": True,
            "maxAttempts": 30,
            "maxParks": 8,
        },
    }
    assert resolved_retry_policy({}) == {
        "enabled": True,
        "maxRetries": 3,
        "baseDelayMs": 2000,
        "maxRetryDelayMs": 60000,
        "waitForUsage": {
            "enabled": True,
            "pauseUntilReset": True,
            "maxAttempts": 30,
            "maxParks": 8,
        },
        "providerBackupModel": "",
    }
