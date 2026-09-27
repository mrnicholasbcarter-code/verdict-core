from __future__ import annotations

import json
from pathlib import Path

from verdict.orchestration.prime_settings import (
    effective_prime_settings,
    one_shot_prime_settings,
    prime_retry_policy_problems,
)


def test_project_prime_settings_do_not_decide_retry_policy() -> None:
    """The tracked project layer applies to every session in this repo, so it
    must not carry retry keys; Verdict-owned launches get one-shot retry from a
    per-launch agent dir instead (see verdict/orchestration/prime_settings.py).
    """
    settings = json.loads(Path(".prime/agent/settings.json").read_text(encoding="utf-8"))

    assert settings.get("providerBackupModel") == ""
    assert "retry" not in settings


def test_verdict_launch_is_one_shot_under_the_tracked_project_file(tmp_path: Path) -> None:
    """Verdict stays the sole worker retry authority: the effective settings of a
    Verdict launch in this checkout make at most one model request.
    """
    config_dir = tmp_path / "agent"
    config_dir.mkdir()
    (config_dir / "settings.json").write_text(
        json.dumps(one_shot_prime_settings()), encoding="utf-8"
    )

    effective = effective_prime_settings(cwd=Path.cwd(), config_dir=config_dir)

    assert prime_retry_policy_problems(effective) == []
