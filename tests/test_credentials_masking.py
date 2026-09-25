"""Test that secrets never leak in any output."""

from __future__ import annotations

from pathlib import Path

import pytest

from verdict.credentials_store import CredentialsStore


def test_secret_never_in_list_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets must not appear in plain text in `verdict credentials list` output."""
    store = CredentialsStore(config_dir=tmp_path)
    secret_value = "sk-secret-1234567890abcdefghijklmnopqrstuvwxyz"
    store.set("TEST_SECRET_KEY", secret_value)

    # Simulate command output
    import io
    import sys

    from verdict.cli import cmd_credentials_list

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)

    try:
        # Need to inject the store
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.parent))
        cmd_credentials_list(output_json=False)
    except SystemExit:
        pass

    output = captured.getvalue()

    # The full secret must not appear anywhere in output
    assert secret_value not in output, "Full secret leaked in list output"
    # No meaningful prefix of the secret (>=8 chars) should appear
    for prefix_len in range(8, len(secret_value) + 1):
        assert secret_value[:prefix_len] not in output, (
            f"Secret prefix of length {prefix_len} leaked in list output"
        )
    # Masked format must be present — no chars, just length
    assert "set (len=" in output, "Expected masked format 'set (len=N)' not found"


def test_secret_never_in_yaml(tmp_path: Path) -> None:
    """Secrets must never be written to verdict.yaml."""
    config_dir = tmp_path / "verdict"
    config_dir.mkdir()
    config_file = config_dir / "verdict.yaml"

    # Create a mock config
    config_file.write_text("primary_model: gpt-4\nproviders: {}\n")

    store = CredentialsStore(config_dir=config_dir)
    secret = "sk-very-secret-key-12345"
    store.set("TEST_KEY", secret)

    # Verify secret is in credentials.env, not in verdict.yaml
    creds_file = config_dir / "credentials.env"
    assert creds_file.exists()
    assert secret in creds_file.read_text()

    assert secret not in config_file.read_text(), "Secret leaked into verdict.yaml"


def test_secret_not_in_command_args(tmp_path: Path) -> None:
    """Setting a secret via --stdin never exposes it in argv."""
    # This test verifies the design: we never accept secrets as command args
    # The implementation uses getpass() or --stdin, never a positional arg

    # If someone tries to pass it as an arg, it would be rejected
    # This is enforced by the argparse schema (no value= argument)
    import argparse

    from verdict.commands.parsers_credentials import register

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register(subparsers)

    # Parse "credentials set MYKEY"
    args = parser.parse_args(["credentials", "set", "MYKEY"])

    # Verify there's no way to pass the secret via CLI args
    assert not hasattr(args, "value"), "Secret value should not be in args"
    assert args.name == "MYKEY"
    assert hasattr(args, "stdin"), "Should have --stdin option"


def test_masked_value_in_doctor_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Doctor output must show only masked credentials."""
    store = CredentialsStore(config_dir=tmp_path)
    secret = "sk-extremely-secret-api-key-xyz123"
    store.set("DOCTOR_TEST_SECRET", secret)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.parent))

    # We can't easily run full doctor here, but we can test get_credential_source
    from verdict.credentials_store import get_credential_source

    _source, masked = get_credential_source("DOCTOR_TEST_SECRET", store)

    assert secret not in masked, "Full secret in masked output"
    # No meaningful prefix of the secret (>=4 chars) should appear
    # (short prefixes like "sk-" can collide with "set (len=N)")
    for prefix_len in range(4, min(16, len(secret) + 1)):
        assert secret[:prefix_len] not in masked, (
            f"Secret prefix of length {prefix_len} leaked in masked value"
        )
    # Masked format must be present — no chars, just length
    assert "set (len=" in masked, "Expected masked format 'set (len=N)' not found"


def test_store_permissions_error_message_safe(tmp_path: Path) -> None:
    """Permission error messages should not leak credential values."""
    store = CredentialsStore(config_dir=tmp_path)
    store.set("PERM_TEST_KEY", "secret123")

    # Make file world-readable
    store.store_path.chmod(0o644)

    try:
        store.load()
        pytest.fail("Should have raised PermissionError")
    except PermissionError as e:
        error_msg = str(e)
        # Error message should not contain credential values
        assert "secret123" not in error_msg
        # But should contain repair command
        assert "chmod 0600" in error_msg


def test_live_check_exception_does_not_leak_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live check exception text must not contain the secret value."""

    from verdict.credentials_registry import _check_omniroute_api_key

    secret = "sk-super-secret-api-key-abcdefghij"
    # Set base URL so check proceeds past the "not set" guard
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://localhost:0")

    # This will fail with a connection error; the error message must not include the secret
    ok, msg = _check_omniroute_api_key(secret)
    assert not ok
    assert secret not in msg, f"Secret leaked in live-check error: {msg!r}"
    # Must be just an exception class name (e.g. ConnectError, ConnectionError)
    assert len(msg) < 80, f"Error message suspiciously long (may contain URL/headers): {msg!r}"
    assert " " not in msg or msg.startswith("HTTP"), (
        f"Error message should be class name or 'HTTP N', got: {msg!r}"
    )


def _make_setup_config(tmp_path: Path) -> None:
    """Helper: create a minimal existing verdict.yaml in tmp_path."""
    cfg_dir = tmp_path / "verdict"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "verdict.yaml").write_text("primary_model: gpt-4\n")


def test_setup_existing_install_mentions_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """verdict setup on an existing install must show the 'setup credentials' hint.

    With all defaults (no non_interactive, no dry_run, no plan_only), cmd_setup
    reaches the existing-config early-return block and shows the hint panel.
    """
    from rich.prompt import Prompt

    from verdict import cli

    _make_setup_config(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Answer "n" to the offer — we only want to check the hint text
    monkeypatch.setattr(Prompt, "ask", lambda *a, **kw: "n")

    cli.cmd_setup()
    out = capsys.readouterr().out
    assert "verdict setup credentials" in out, (
        "Existing-install panel must mention 'verdict setup credentials'"
    )


def test_setup_existing_install_non_interactive_does_not_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_setup(non_interactive=True) must never call Prompt.ask in the existing-install path.

    The non_interactive flag routes through wants_bootstrap_plan so the existing-config
    block is not reached; but if it is, no prompt must fire.
    """
    from rich.prompt import Prompt

    _make_setup_config(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def _fail_if_prompted(*args: object, **kwargs: object) -> str:
        raise AssertionError("Prompt.ask called in non-interactive mode")

    monkeypatch.setattr(Prompt, "ask", _fail_if_prompted)
    # Use cmd_setup() with no flags — then monkeypatch non_interactive into the
    # early-return logic by calling cmd_setup_credentials-guarded path directly.
    # Directly invoke the existing-install code path with non_interactive=True.
    # That path only reaches Prompt.ask when non_interactive=False, so this must pass.
    import verdict.cli as _cli

    def _no_creds(**kw: object) -> None:
        pass  # prevent actual credentials step from running

    monkeypatch.setattr(_cli, "cmd_setup_credentials", _no_creds)
    # With non_interactive=True, cmd_setup routes through wants_bootstrap_plan
    # and never reaches the existing-install early-return block; Prompt.ask is never called.
    _cli.cmd_setup(non_interactive=True)


def test_setup_existing_install_interactive_offers_credentials_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In interactive mode, cmd_setup offers to run credentials step (default n)."""
    from unittest.mock import MagicMock

    from rich.prompt import Prompt

    from verdict import cli

    _make_setup_config(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    # Answer "n" so credentials step doesn't actually run
    monkeypatch.setattr(Prompt, "ask", lambda *a, **kw: "n")

    credentials_step_called = MagicMock()
    monkeypatch.setattr(cli, "cmd_setup_credentials", credentials_step_called)

    cli.cmd_setup()
    credentials_step_called.assert_not_called()


def test_setup_existing_install_interactive_runs_credentials_on_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In interactive mode, answering 'y' runs the credentials step."""
    from unittest.mock import MagicMock

    from rich.prompt import Prompt

    from verdict import cli

    _make_setup_config(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    monkeypatch.setattr(Prompt, "ask", lambda *a, **kw: "y")

    credentials_step_called = MagicMock()
    monkeypatch.setattr(cli, "cmd_setup_credentials", credentials_step_called)

    cli.cmd_setup()
    credentials_step_called.assert_called_once_with(non_interactive=False)
