from __future__ import annotations

import json
from pathlib import Path


def test_project_prime_does_not_retry_provider_failures_behind_verdict() -> None:
    settings = json.loads(Path(".prime/agent/settings.json").read_text(encoding="utf-8"))

    assert settings.get("providerBackupModel") == ""
    retry = settings.get("retry")
    assert isinstance(retry, dict)
    assert retry.get("enabled") is False
