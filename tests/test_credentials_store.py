"""Tests for credentials_store.py functionality."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from verdict.credentials_store import CredentialsStore, get_credential_source


@pytest.fixture
def temp_config_dir(tmp_path: Path) -> Path:
    """Provide a temporary config directory."""
    return tmp_path / "test_verdict_config"


@pytest.fixture
def store(temp_config_dir: Path) -> CredentialsStore:
    """Provide a CredentialsStore with a temp directory."""
    return CredentialsStore(config_dir=temp_config_dir)


def test_management_resolver_precedence_and_named_field(monkeypatch, store):
    from verdict import credentials_store as module

    store.set("OMNIROUTE_MANAGEMENT_TOKEN", " stored-sentinel ")
    store.set("OMNIROUTE_API_KEY", "not-management")
    monkeypatch.setattr(module, "CredentialsStore", lambda: store)
    assert (
        module.resolve_management_token({"OMNIROUTE_MANAGEMENT_TOKEN": " env-sentinel "})
        == "env-sentinel"
    )
    assert (
        module.resolve_management_token({"OMNIROUTE_MANAGEMENT_TOKEN": "  "}) == "stored-sentinel"
    )
    store.unset("OMNIROUTE_MANAGEMENT_TOKEN")
    assert module.resolve_management_token({}) is None
    store.store_path.chmod(0o644)
    assert module.resolve_management_token({}) is None


def test_management_token_source_records_real_provenance(monkeypatch, store):
    """BOD-219: provenance comes from what load_into_env injected, not value equality."""
    from verdict import credentials_store as module

    monkeypatch.setattr(module, "_STORE_INJECTED", {})
    monkeypatch.setattr(module, "CredentialsStore", lambda: store)
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN", raising=False)
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", "same-sentinel")
    assert module.resolve_management_token_source() == ("same-sentinel", "credential_store")
    # An explicit export equal to the stored value is still an export.
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "same-sentinel")
    assert module.resolve_management_token_source() == ("same-sentinel", "environment")
    # A value load_into_env copied from the store stays the store's.
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN")
    store.load_into_env()
    assert os.environ["OMNIROUTE_MANAGEMENT_TOKEN"] == "same-sentinel"
    assert module.resolve_management_token_source() == ("same-sentinel", "credential_store")
    # Overwriting the copied value in-process makes it an export again.
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "other-sentinel")
    assert module.resolve_management_token_source() == ("other-sentinel", "environment")
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "  ")
    assert module.resolve_management_token_source() == ("same-sentinel", "credential_store")
    store.unset("OMNIROUTE_MANAGEMENT_TOKEN")
    assert module.resolve_management_token_source() is None


def test_repeated_load_into_env_records_only_the_latest_load(monkeypatch, store):
    """BOD-219 review: each load resets the record; a name not re-injected is dropped."""
    from verdict import credentials_store as module

    monkeypatch.setattr(module, "_STORE_INJECTED", {})
    monkeypatch.setattr(module, "CredentialsStore", lambda: store)
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN", raising=False)
    monkeypatch.delenv("UNRELATED_SENTINEL", raising=False)
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", "first-sentinel")
    store.load_into_env()
    assert module.injected_from_store("OMNIROUTE_MANAGEMENT_TOKEN")
    # The store no longer holds the token; the next load does not re-inject it.
    store.unset("OMNIROUTE_MANAGEMENT_TOKEN")
    store.set("UNRELATED_SENTINEL", "x")
    store.load_into_env()
    assert not module.injected_from_store("OMNIROUTE_MANAGEMENT_TOKEN")
    assert set(module._STORE_INJECTED) == {"UNRELATED_SENTINEL"}
    assert module.resolve_management_token_source() == ("first-sentinel", "environment")


def test_limitation_identical_value_reexported_in_process_reads_as_credential_store(
    monkeypatch, store
):
    """Documented limitation, not a goal: an in-process re-export of the exact value
    the latest load copied in cannot be told apart from that copy."""
    from verdict import credentials_store as module

    monkeypatch.setattr(module, "_STORE_INJECTED", {})
    monkeypatch.setattr(module, "CredentialsStore", lambda: store)
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN", raising=False)
    store.set("OMNIROUTE_MANAGEMENT_TOKEN", "same-sentinel")
    store.load_into_env()
    monkeypatch.delenv("OMNIROUTE_MANAGEMENT_TOKEN")
    monkeypatch.setenv("OMNIROUTE_MANAGEMENT_TOKEN", "same-sentinel")
    assert module.resolve_management_token_source() == ("same-sentinel", "credential_store")


def test_store_creates_secure_directory(store: CredentialsStore) -> None:
    """Store creates config directory with 0700 permissions."""
    store.set("TEST_KEY", "test_value")

    assert store.config_dir.exists()
    dir_stat = store.config_dir.stat()
    mode = stat.S_IMODE(dir_stat.st_mode)

    # Directory should be 0700
    assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"


def test_store_creates_secure_file(store: CredentialsStore) -> None:
    """Store creates credentials.env with 0600 permissions."""
    store.set("TEST_KEY", "test_value")

    assert store.store_path.exists()
    file_stat = store.store_path.stat()
    mode = stat.S_IMODE(file_stat.st_mode)

    # File should be 0600
    assert mode == 0o600, f"Expected 0o600, got {oct(mode)}"


def test_store_refuses_insecure_permissions(store: CredentialsStore) -> None:
    """Store refuses to load when file has group/world permissions."""
    store.set("TEST_KEY", "test_value")

    # Make file world-readable
    store.store_path.chmod(0o644)

    with pytest.raises(PermissionError, match="insecure permissions"):
        store.load()


def test_store_suggests_chmod_repair(store: CredentialsStore) -> None:
    """Error message includes exact chmod repair command."""
    store.set("TEST_KEY", "test_value")
    store.store_path.chmod(0o644)

    with pytest.raises(PermissionError) as exc_info:
        store.load()

    error_msg = str(exc_info.value)
    assert "chmod 0600" in error_msg
    assert str(store.store_path) in error_msg


def test_store_set_and_load(store: CredentialsStore) -> None:
    """Store can set and load credentials."""
    store.set("KEY_ONE", "value_one")
    store.set("KEY_TWO", "value_two")

    loaded = store.load()
    assert loaded == {"KEY_ONE": "value_one", "KEY_TWO": "value_two"}


def test_store_unset(store: CredentialsStore) -> None:
    """Store can unset credentials."""
    store.set("KEY_ONE", "value_one")
    store.set("KEY_TWO", "value_two")

    assert store.unset("KEY_ONE") is True
    loaded = store.load()
    assert loaded == {"KEY_TWO": "value_two"}


def test_store_unset_nonexistent(store: CredentialsStore) -> None:
    """Unsetting a nonexistent key returns False."""
    assert store.unset("NONEXISTENT") is False


def test_store_unset_last_removes_file(store: CredentialsStore) -> None:
    """Unsetting the last credential removes the file."""
    store.set("KEY_ONE", "value_one")
    store.unset("KEY_ONE")

    assert not store.store_path.exists()


def test_store_atomic_write(store: CredentialsStore) -> None:
    """Store uses atomic writes."""
    store.set("KEY_ONE", "value_one")

    # Set again - should use atomic replace
    store.set("KEY_TWO", "value_two")

    loaded = store.load()
    assert loaded == {"KEY_ONE": "value_one", "KEY_TWO": "value_two"}


def test_load_into_env_respects_precedence(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_into_env: exported env var wins over store."""
    store.set("TEST_KEY", "stored_value")

    # Set environment variable
    monkeypatch.setenv("TEST_KEY", "env_value")

    store.load_into_env()

    # Env value should win
    assert os.environ["TEST_KEY"] == "env_value"


def test_load_into_env_fills_unset(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """load_into_env fills unset names from store."""
    store.set("UNSET_KEY", "stored_value")

    # Ensure not in env
    monkeypatch.delenv("UNSET_KEY", raising=False)

    store.load_into_env()

    # Should be filled from store
    assert os.environ.get("UNSET_KEY") == "stored_value"


def test_get_credential_source_from_env(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_credential_source detects env source."""
    monkeypatch.setenv("TEST_KEY", "env_value")

    source, masked = get_credential_source("TEST_KEY", store)
    assert source == "env"
    # New masking policy: no characters shown, only "set (len=N)"
    assert "env_value" not in masked
    assert "set (len=" in masked


def test_get_credential_source_from_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_credential_source detects store source."""
    store.set("TEST_KEY", "stored_value")
    monkeypatch.delenv("TEST_KEY", raising=False)

    source, masked = get_credential_source("TEST_KEY", store)
    assert source == "store"
    # New masking policy: no characters shown, only "set (len=N)"
    assert "stored_value" not in masked
    assert "set (len=" in masked


def test_get_credential_source_names_a_value_load_into_env_copied_as_the_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doctor Credentials list reads provenance from the injection record.

    The CLI entry runs ``load_into_env`` first, so a stored value sits in the
    environment; it is the store's while it equals the value the latest load copied.
    """
    from verdict import credentials_store as module

    monkeypatch.setattr(module, "_STORE_INJECTED", {})
    # setenv then delenv registers TEST_KEY for cleanup after load_into_env sets it.
    monkeypatch.setenv("TEST_KEY", "placeholder")
    monkeypatch.delenv("TEST_KEY")
    store.set("TEST_KEY", "stored_value")
    store.load_into_env()
    assert os.environ["TEST_KEY"] == "stored_value"
    source, masked = get_credential_source("TEST_KEY", store)
    assert source == "store"
    assert "stored_value" not in masked
    # An in-process overwrite after the copy is an export again.
    monkeypatch.setenv("TEST_KEY", "other_value")
    assert get_credential_source("TEST_KEY", store)[0] == "env"
    # An export made before the load, equal to the stored value, stays an export.
    monkeypatch.setenv("TEST_KEY", "stored_value")
    monkeypatch.setattr(module, "_STORE_INJECTED", {})
    store.load_into_env()
    assert get_credential_source("TEST_KEY", store)[0] == "env"


def test_limitation_value_changed_away_and_back_in_process_lists_as_store(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Documented limitation, not a goal: the record holds only a digest of the latest
    copy, so a value changed away and then set back to that exact string in the same
    process cannot be told apart from the copy and lists as ``store``."""
    from verdict import credentials_store as module

    monkeypatch.setattr(module, "_STORE_INJECTED", {})
    monkeypatch.setenv("TEST_KEY", "placeholder")
    monkeypatch.delenv("TEST_KEY")
    store.set("TEST_KEY", "stored_value")
    store.load_into_env()
    monkeypatch.setenv("TEST_KEY", "other_value")
    assert get_credential_source("TEST_KEY", store)[0] == "env"
    monkeypatch.setenv("TEST_KEY", "stored_value")
    assert get_credential_source("TEST_KEY", store)[0] == "store"


def test_get_credential_source_missing(
    store: CredentialsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_credential_source reports missing."""
    monkeypatch.delenv("TEST_KEY", raising=False)

    source, masked = get_credential_source("TEST_KEY", store)
    assert source == "missing"
    assert masked == ""


def test_masking_hides_secrets(store: CredentialsStore) -> None:
    """Masked values do not reveal full secrets."""
    secret = "sk-1234567890abcdefghijklmnop"
    store.set("SECRET_KEY", secret)

    _source, masked = get_credential_source("SECRET_KEY", store)

    # Should not contain the full secret or any prefix
    assert secret not in masked
    # New masking policy: "set (len=N)" format, no characters of the secret
    assert "set (len=" in masked
    assert secret[:4] not in masked


def test_store_survives_empty_lines_and_comments(store: CredentialsStore) -> None:
    """Store handles empty lines and comments gracefully."""
    store.set("KEY_ONE", "value_one")

    # Manually add comments
    with open(store.store_path, "a") as f:
        f.write("\n# This is a comment\n\n")

    store.set("KEY_TWO", "value_two")

    loaded = store.load()
    assert "KEY_ONE" in loaded
    assert "KEY_TWO" in loaded


def test_resolve_gateway_binding_normalizes_url_and_uses_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The canonical binding combines the normalized gateway with stored credentials."""
    from verdict.credentials_store import resolve_gateway_binding

    config_dir = tmp_path / "config" / "verdict"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128/")
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    CredentialsStore(config_dir=config_dir).set("VERDICT_OMNIROUTE_API_KEY", "stored-fixture-key")

    assert resolve_gateway_binding() == ("http://127.0.0.1:20128/v1", "stored-fixture-key")


def test_resolve_gateway_binding_explicit_url_and_default_are_canonical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Explicit URL wins, and an absent setting uses the supported 20128 gateway."""
    from verdict.credentials_store import resolve_gateway_binding

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.delenv("LLMGATE_UPSTREAM_BASE_URL", raising=False)
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)

    assert resolve_gateway_binding(base_url="http://gateway.example/custom") == (
        "http://gateway.example/custom/v1",
        None,
    )
    assert resolve_gateway_binding() == ("http://127.0.0.1:20128/v1", None)


def test_cli_gate_bootstrap_uses_canonical_stored_gateway_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shared CLI gate sees the canonical stored key and gateway binding."""
    from verdict.actions.helpers import _cli_bootstrap

    config_dir = tmp_path / "config" / "verdict"
    config_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.delenv("VERDICT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    CredentialsStore(config_dir=config_dir).set("VERDICT_OMNIROUTE_API_KEY", "stored-fixture-key")

    bootstrap = _cli_bootstrap()

    assert bootstrap.providers["omniroute"].base_url == "http://127.0.0.1:20128/v1"
    assert bootstrap.providers["omniroute"].api_key_env == "OMNIROUTE_API_KEY"
    assert "credential_env_missing" not in {note.code for note in bootstrap.notes()}
