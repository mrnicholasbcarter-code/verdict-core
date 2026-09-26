"""S2-A: ``verdict harness prime sync-models`` rewrites only providers.omniroute.models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from verdict.harness_prime import HarnessPrimeError, format_sync_models, sync_models

ORIGINAL = {
    "providers": {
        "omniroute": {
            "baseUrl": "http://127.0.0.1:20128/v1",
            "api": "openai-completions",
            "apiKey": "OMNIROUTE_KEY_ENV",
            "models": [
                {"id": "cc/keep", "name": "keep", "thinkingLevelMap": {"high": "high"}},
                {"id": "cc/gone", "name": "gone"},
            ],
        },
        "ollama": {"baseUrl": "http://127.0.0.1:11434/v1", "models": [{"id": "llama"}]},
    },
    "other": {"untouched": True},
}
LIVE = [
    {"id": "cc/keep", "owned_by": "claude"},
    {
        "id": "cc/new",
        "owned_by": "claude",
        "capabilities": {"reasoning": True},
        "max_input_tokens": 200000,
        "max_output_tokens": 64000,
    },
]


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "agent"
    home.mkdir()
    (home / "models.json").write_text(json.dumps(ORIGINAL, indent=2), encoding="utf-8")
    return home


def test_dry_run_reports_diff_and_writes_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    before = (home / "models.json").read_bytes()
    result = sync_models(LIVE, dry_run=True, prime_home=home)
    assert result.added == ("cc/new",)
    assert result.removed == ("cc/gone",)
    assert result.written is False and result.backup_path is None
    assert (home / "models.json").read_bytes() == before
    assert sorted(p.name for p in home.iterdir()) == ["models.json"]
    text = format_sync_models(result)
    assert "dry-run" in text and "added: 1" in text and "removed: 1" in text


def test_sync_writes_only_omniroute_models_and_leaves_backup(tmp_path: Path) -> None:
    home = _home(tmp_path)
    before = (home / "models.json").read_bytes()
    result = sync_models(LIVE, prime_home=home, now=lambda: "20260926T000000Z")
    assert result.written is True
    backup = home / "models.json.verdict-sync-20260926T000000Z.bak"
    assert result.backup_path == backup
    assert backup.read_bytes() == before
    data = json.loads((home / "models.json").read_text())
    omni = data["providers"]["omniroute"]
    assert [m["id"] for m in omni["models"]] == ["cc/keep", "cc/new"]
    # existing per-model settings survive; new entries get sane defaults
    assert omni["models"][0]["thinkingLevelMap"] == {"high": "high"}
    new = omni["models"][1]
    assert new["reasoning"] is True and new["contextWindow"] == 200000
    assert new["maxTokens"] == 64000
    # everything outside providers.omniroute.models is unchanged
    for key in ("baseUrl", "api", "apiKey"):
        assert omni[key] == ORIGINAL["providers"]["omniroute"][key]  # type: ignore[index]
    assert data["providers"]["ollama"] == ORIGINAL["providers"]["ollama"]  # type: ignore[index]
    assert data["other"] == {"untouched": True}


def test_sync_refuses_empty_inventory_and_missing_provider(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with pytest.raises(HarnessPrimeError):
        sync_models([], prime_home=home)
    (home / "models.json").write_text(json.dumps({"providers": {}}), encoding="utf-8")
    with pytest.raises(HarnessPrimeError):
        sync_models(LIVE, prime_home=home)
    with pytest.raises(HarnessPrimeError):
        sync_models(LIVE, prime_home=tmp_path / "nowhere")


def test_cli_dry_run_is_wired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    from verdict.orchestration import cli as orch_cli
    from verdict.orchestration import run as orch_run

    home = _home(tmp_path)
    monkeypatch.setenv("PRIME_AGENT_HOME", str(home))
    seen: list[str] = []

    def fake_inventory(gateway: str, *, api_key: str | None) -> list[dict[str, object]]:
        seen.append(gateway)
        return list(LIVE)

    monkeypatch.setattr(orch_run, "fetch_inventory", fake_inventory)
    before = (home / "models.json").read_bytes()
    args = argparse.Namespace(
        command="harness",
        harness_target="prime",
        harness_prime_command="sync-models",
        gateway="http://127.0.0.1:20128/v1",
        dry_run=True,
    )
    assert orch_cli.dispatch(args) == 0
    assert seen == ["http://127.0.0.1:20128"]
    out = capsys.readouterr().out
    assert "added: 1" in out and "removed: 1" in out
    assert (home / "models.json").read_bytes() == before


def test_cli_help_lists_sync_models(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    from verdict import cli

    monkeypatch.setattr(sys, "argv", ["verdict", "harness", "prime", "sync-models", "--help"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert "--dry-run" in capsys.readouterr().out
