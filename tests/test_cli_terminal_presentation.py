"""CLI integration: presentation must not corrupt existing state or JSON."""

import json
from pathlib import Path

import pytest

from verdict import cli


def test_setup_plan_uses_shared_header(capsys):
    cli.cmd_setup_plan()
    output = capsys.readouterr().out
    assert "VERDICT" in output
    assert "No changes made" in output
    assert "\x1b" not in output


def test_setup_recommended_renders_actual_recommendations(monkeypatch, capsys):
    import verdict.capability_bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "discover_providers", lambda **kwargs: ())
    cli.cmd_setup(recommended=True)
    output = capsys.readouterr().out
    assert "Recommendations" in output
    assert "code.symbols" in output
    assert "Plan" in output


def test_apply_json_never_prompts_or_animates(monkeypatch, tmp_path, capsys):
    import verdict.capability_bootstrap as bootstrap

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(bootstrap, "discover_providers", lambda **kwargs: ())
    with pytest.raises(SystemExit) as error:
        cli.cmd_setup(apply=True, output_json=True)
    assert error.value.code == 2
    output = capsys.readouterr().out
    assert json.loads(output)["apply"]["mutated"] is False
    assert "\x1b" not in output
