"""Prime launch settings precedence: the EFFECTIVE one-shot retry policy floor.

These tests model Prime 0.9.6's real merge (``global`` then ``project``, project
wins, nested objects merge one level deep) and assert the settings a launch
would actually apply — not just the contents of Verdict's isolated config dir.
Prime 0.9.6 has no per-launch escape from project precedence, so the tracked
project file must itself be one-shot and the launch must refuse to run when it
is not.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.orchestration.prime_settings import (
    PROJECT_SETTINGS_RELPATH,
    PrimeRetryPolicyError,
    assert_one_shot_launch,
    effective_prime_settings,
    merge_prime_settings,
    one_shot_prime_settings,
    prime_retry_policy_problems,
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


def test_tracked_project_settings_are_themselves_one_shot() -> None:
    """Real launches run in this repo (or a git worktree of it), so the tracked
    project file is part of every effective merge and must not re-enable retry."""
    tracked = json.loads(TRACKED_PROJECT_SETTINGS.read_text(encoding="utf-8"))
    assert prime_retry_policy_problems(tracked) == []


def test_real_launch_in_this_repo_has_retries_off(tmp_path: Path) -> None:
    """End-to-end precedence check against the committed project file."""
    config_dir = tmp_path / "launch-config"
    config_dir.mkdir()
    _write(config_dir / "settings.json", one_shot_prime_settings())
    effective = effective_prime_settings(cwd=ROOT, config_dir=config_dir)
    assert effective["retry"]["enabled"] is False
    assert effective["retry"]["maxRetries"] == 0
    wait = effective["retry"]["provider"]["waitForUsage"]
    assert wait["enabled"] is False
    assert wait["pauseUntilReset"] is False
    assert effective["providerBackupModel"] == ""
    assert assert_one_shot_launch(cwd=ROOT, config_dir=config_dir) == effective
