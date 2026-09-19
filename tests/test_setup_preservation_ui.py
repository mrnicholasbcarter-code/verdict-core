from pathlib import Path

from verdict import cli


def test_existing_configuration_is_preserved_without_replaying_wizard(
    monkeypatch, tmp_path, capsys
):
    config_dir = tmp_path / "config" / "verdict"
    config_dir.mkdir(parents=True)
    config = config_dir / "verdict.yaml"
    original = "primary_model: keep-me\ncustom_setting: keep-this-too\n"
    config.write_text(original)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    def no_prompt(*args, **kwargs):
        raise AssertionError("A completed configuration must not replay the wizard")

    monkeypatch.setattr(cli.Prompt, "ask", no_prompt)
    cli.cmd_setup()
    assert config.read_text() == original
    assert "preserved" in capsys.readouterr().out.lower()
