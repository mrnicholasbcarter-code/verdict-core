"""BOD-385: every OmniRoute transport site resolves the gateway key via credentials_store."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from verdict import api, worker_runtime
from verdict.orchestration import run as orchestration_run

FILE_KEY = "file-key-fixture"


class _RecordedError(Exception):
    pass


@pytest.fixture
def key_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = tmp_path / "config"
    (config / "verdict").mkdir(parents=True, mode=0o700)
    path = config / "verdict" / "omniroute-api-key"
    path.write_text(FILE_KEY + "\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))


def _recorder(seen: list[str | None]) -> Any:
    def build(*args: Any, api_key: str | None = None, **kwargs: Any) -> Any:
        seen.append(api_key)
        raise _RecordedError

    return build


def test_orchestration_resolve_api_key_reads_owner_only_key_file(key_file: None) -> None:
    assert orchestration_run.resolve_api_key() == FILE_KEY


def test_subagent_selector_uses_key_file(key_file: None, monkeypatch: pytest.MonkeyPatch) -> None:
    import verdict.omniroute
    from verdict.subagent_models import SubagentModelSelector

    seen: list[str | None] = []
    monkeypatch.setattr(verdict.omniroute, "OmniRouteHTTPTransport", _recorder(seen))
    with pytest.raises(_RecordedError):
        SubagentModelSelector.from_environment()
    assert seen == [FILE_KEY]


def test_api_availability_cache_uses_key_file(
    key_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/v1")
    seen: list[str | None] = []
    monkeypatch.setattr(api, "OmniRouteHTTPTransport", _recorder(seen))
    with pytest.raises(RuntimeError):
        api._build_availability_cache()
    assert seen == [FILE_KEY]


def test_worker_cli_uses_key_file(
    key_file: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str | None] = []
    monkeypatch.setattr(worker_runtime, "OmniRouteHTTPTransport", _recorder(seen))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"prompt": "p", "task": {}}))
    assert asyncio.run(worker_runtime.cli_run(run_dir)) == 1
    assert seen == [FILE_KEY]


def test_verdict_env_key_takes_precedence_over_key_file(
    key_file: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_API_KEY", "generic-env-key")
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "verdict-env-key")
    assert orchestration_run.resolve_api_key() == "verdict-env-key"
