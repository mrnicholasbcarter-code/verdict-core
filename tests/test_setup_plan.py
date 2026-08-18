"""Tests for auditable, mutation-free setup plans."""

from __future__ import annotations

from pathlib import Path

from verdict.setup_plan import build_setup_plan


def test_setup_actions_explain_reason_security_postcondition_and_undo(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    report = build_setup_plan().to_dict()
    action = report["actions"][0]

    for field in ("reason", "security_impact", "postcondition", "undo"):
        assert isinstance(action[field], str)
        assert action[field]
    assert report["mutation_free"] is True
    assert not (tmp_path / "config").exists()


def test_existing_config_preview_explains_noop_without_reading_contents(
    tmp_path: Path, monkeypatch
) -> None:
    config_dir = tmp_path / "config" / "verdict"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "verdict.yaml"
    config_path.write_text("private: do-not-read\n", encoding="utf-8")
    before = config_path.read_bytes()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    action = build_setup_plan().to_dict()["actions"][0]

    assert action["action_id"] == "preserve-config"
    assert "byte-for-byte" in action["postcondition"]
    assert config_path.read_bytes() == before
