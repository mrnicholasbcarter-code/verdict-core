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


def test_setup_dry_run_preserves_existing_config(monkeypatch, tmp_path):
    """BOD-414 / S11: unit installers plan the migration, keep the old unit for
    rollback before replacing it, and the census copy records its build."""
    import io

    from rich.console import Console

    from verdict import acceptance_service, census_passports_service, prove_at_rest_service

    install_prove = prove_at_rest_service.manage_service
    from verdict.census_passports_service import package_build
    from verdict.home_setup_render import _warm_cache
    from verdict.terminal_ui import TerminalUI

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    units = tmp_path / "units"
    units.mkdir()
    stranded = (
        "[Service]\nWorkingDirectory=/home/x/worktrees/stranded\n"
        'ExecStart="/home/x/worktrees/stranded/.venv/bin/python" -m verdict acceptance run\n'
    )
    (units / acceptance_service.SERVICE).write_text(stranded)
    calls: list[list[str]] = []

    # 1. Dry-run plans the migration and touches nothing.
    plan = acceptance_service.manage_service(unit_dir=units, dry_run=True, run=calls.append)
    assert plan["plan"] == {
        acceptance_service.SERVICE: "replace",
        acceptance_service.TIMER: "create",
    }
    assert plan["rollback"] is None
    assert (units / acceptance_service.SERVICE).read_text() == stranded
    assert not (units / acceptance_service.TIMER).exists()
    assert calls == [] and not (tmp_path / "state").exists()

    # 2. Declining the setup step keeps the existing unit and state untouched.
    monkeypatch.setattr(
        prove_at_rest_service,
        "timer_state",
        lambda unit: {"state": "active", "installed": True, "failed": False, "active": True},
    )
    ui = TerminalUI(Console(file=io.StringIO(), width=120, force_terminal=False))
    notes: list[str] = []
    _warm_cache(ui, iter(["on", "n", "off", "n"]).__next__, "http://gw", notes)
    assert (units / acceptance_service.SERVICE).read_text() == stranded
    assert not (tmp_path / "state").exists()

    # 2b. Confirming the step reports the unit plan and where the old unit was saved.
    monkeypatch.setattr(
        prove_at_rest_service,
        "manage_service",
        lambda **_kwargs: {
            "changed": True,
            "plan": {prove_at_rest_service.SERVICE: "replace"},
            "rollback": str(tmp_path / "state" / "service-rollback" / "t"),
        },
    )
    confirmed: list[str] = []
    _warm_cache(ui, iter(["on", "y", "off", "n"]).__next__, "http://gw", confirmed)
    assert any("service-rollback" in note for note in confirmed)
    assert f"{prove_at_rest_service.SERVICE} replace" in ui.console.file.getvalue()

    # 3. The approved install saves the replaced unit before writing the new one.
    applied = acceptance_service.manage_service(unit_dir=units, run=calls.append)
    rollback = Path(applied["rollback"])
    assert (rollback / acceptance_service.SERVICE).read_text() == stranded
    assert rollback.is_relative_to(tmp_path / "state" / "service-rollback")
    assert (units / acceptance_service.SERVICE).read_text() == applied["files"][
        acceptance_service.SERVICE
    ]

    # 4. Re-install is idempotent: nothing to change, nothing new to roll back.
    again = acceptance_service.manage_service(unit_dir=units, run=calls.append)
    assert not again["changed"] and again["rollback"] is None
    assert set(again["plan"].values()) == {"keep"}
    prove = install_prove(unit_dir=units, run=calls.append)
    assert install_prove(unit_dir=units, run=calls.append)["plan"] == {
        name: "keep" for name in prove["files"]
    }

    # 5. The census runtime copy records the build it was copied from.
    source = tmp_path / "src"
    (source / "verdict").mkdir(parents=True)
    (source / "verdict" / "__init__.py").write_text('__version__ = "0.5.0"\n')
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("4f642962894da7ac4c84615eac3bcc4b35f6d1f3\n")
    runtime = tmp_path / "runtime"
    census_passports_service.manage_census_service(
        unit_dir=units, source_root=source, runtime_root=runtime, run=calls.append
    )
    assert package_build(runtime / "repo" / "verdict") == "git 4f642962894d"
    assert package_build(runtime / "repo" / "verdict") == package_build(source / "verdict")
