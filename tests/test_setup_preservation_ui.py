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

    cred_prompt = "Run credentials setup now?"
    prompt_calls: list[str] = []

    def controlled_prompt(question: str, **kw: object) -> str:
        prompt_calls.append(question)
        if question == cred_prompt:
            return "n"
        raise AssertionError(
            f"Wizard prompt fired on an existing install — must not replay: {question!r}"
        )

    monkeypatch.setattr(cli.Prompt, "ask", controlled_prompt)
    cli.cmd_setup()

    assert config.read_text() == original, "Existing config must not be modified"
    assert "preserved" in capsys.readouterr().out.lower()
    assert prompt_calls == [cred_prompt], (
        f"Expected exactly one credentials-offer prompt, got: {prompt_calls!r}"
    )
