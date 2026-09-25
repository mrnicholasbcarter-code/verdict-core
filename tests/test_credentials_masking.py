"""Test that secrets never leak in any output."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from verdict.credentials_store import CredentialsStore


def test_secret_never_in_list_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets must not appear in plain text in `verdict credentials list` output."""
    store = CredentialsStore(config_dir=tmp_path)
    secret_value = "sk-secret-1234567890abcdefghijklmnopqrstuvwxyz"
    store.set("TEST_SECRET_KEY", secret_value)
    
    # Simulate command output
    from verdict.cli import cmd_credentials_list
    import io
    import sys
    
    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    
    try:
        # Need to inject the store
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.parent))
        cmd_credentials_list(output_json=False)
    except SystemExit:
        pass
    
    output = captured.getvalue()
    
    # The full secret must not appear
    assert secret_value not in output, "Full secret leaked in list output"
    # Only masked portion should appear
    assert "sk-secre..." in output or "[len=" in output


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
    from verdict.commands.parsers_credentials import register
    import argparse
    
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
    
    source, masked = get_credential_source("DOCTOR_TEST_SECRET", store)
    
    assert secret not in masked, "Full secret in masked output"
    assert "..." in masked or "[len=" in masked


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
