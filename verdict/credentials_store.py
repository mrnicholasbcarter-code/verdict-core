"""Secure credential storage for verdict.

Stores credentials in $XDG_CONFIG_HOME/verdict/credentials.env with:
- Directory permissions 0700
- File permissions 0600
- Atomic writes via temp file
- Refuses to load when group/world readable
- Never imports at module level; load explicitly in CLI/server entry
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


class CredentialsStore:
    """Secure local credential store."""

    def __init__(self, config_dir: Path | None = None) -> None:
        """Initialize store with config directory."""
        if config_dir is None:
            xdg_config = os.environ.get("XDG_CONFIG_HOME")
            if xdg_config:
                config_dir = Path(xdg_config) / "verdict"
            else:
                config_dir = Path.home() / ".config" / "verdict"

        self.config_dir = config_dir
        self.store_path = config_dir / "credentials.env"

    def _ensure_secure_dir(self) -> None:
        """Ensure config directory exists with 0700 permissions."""
        if not self.config_dir.exists():
            old_umask = os.umask(0o077)
            try:
                self.config_dir.mkdir(parents=True, mode=0o700)
            finally:
                os.umask(old_umask)
        else:
            # Check existing directory permissions
            dir_stat = self.config_dir.stat()
            if dir_stat.st_mode & 0o077:
                # Directory is too permissive, fix it
                self.config_dir.chmod(0o700)

    def _check_file_permissions(self) -> tuple[bool, str]:
        """Check if store file has secure permissions.

        Returns:
            (is_secure, error_message_or_empty)
        """
        if not self.store_path.exists():
            return True, ""

        file_stat = self.store_path.stat()
        mode = file_stat.st_mode

        # Check group and world permissions
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            return False, (
                f"Credential store has insecure permissions: {oct(stat.S_IMODE(mode))}\n"
                f"Fix with: chmod 0600 {self.store_path}"
            )

        return True, ""

    def load(self) -> dict[str, str]:
        """Load credentials from store file.

        Returns dict of env_name -> value.
        Refuses to load if permissions are insecure.
        """
        if not self.store_path.exists():
            return {}

        is_secure, error_msg = self._check_file_permissions()
        if not is_secure:
            raise PermissionError(error_msg)

        credentials = {}
        with open(self.store_path) as f:
            for _line_num, line in enumerate(f, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    # Skip malformed lines
                    continue

                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                if key:
                    credentials[key] = value

        return credentials

    def load_into_env(self) -> None:
        """Load credentials into os.environ, only for unset names.

        Exported env vars always win over stored values.
        """
        stored = self.load()
        for key, value in stored.items():
            if key not in os.environ:
                os.environ[key] = value

    def set(self, env_name: str, value: str) -> None:
        """Set a credential in the store.

        Uses atomic write via temp file + os.replace.
        """
        self._ensure_secure_dir()

        # Load existing
        existing = {}
        if self.store_path.exists():
            is_secure, error_msg = self._check_file_permissions()
            if not is_secure:
                raise PermissionError(error_msg)
            existing = self.load()

        # Update
        existing[env_name] = value

        # Write atomically
        old_umask = os.umask(0o077)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.config_dir, delete=False, prefix=".credentials.env."
            ) as tmp:
                tmp_path = Path(tmp.name)
                for key in sorted(existing.keys()):
                    tmp.write(f"{key}={existing[key]}\n")

            # Ensure temp file has 0600
            tmp_path.chmod(0o600)

            # Atomic replace
            tmp_path.replace(self.store_path)
        finally:
            os.umask(old_umask)

    def unset(self, env_name: str) -> bool:
        """Remove a credential from the store.

        Returns True if it was present and removed.
        """
        if not self.store_path.exists():
            return False

        is_secure, error_msg = self._check_file_permissions()
        if not is_secure:
            raise PermissionError(error_msg)

        existing = self.load()
        if env_name not in existing:
            return False

        del existing[env_name]

        if not existing:
            # Remove empty file
            self.store_path.unlink()
            return True

        # Write remaining credentials
        self._ensure_secure_dir()
        old_umask = os.umask(0o077)
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.config_dir, delete=False, prefix=".credentials.env."
            ) as tmp:
                tmp_path = Path(tmp.name)
                for key in sorted(existing.keys()):
                    tmp.write(f"{key}={existing[key]}\n")

            tmp_path.chmod(0o600)
            tmp_path.replace(self.store_path)
        finally:
            os.umask(old_umask)

        return True

    def list_credentials(self) -> dict[str, str]:
        """List all credentials in the store.

        Returns dict of env_name -> value.
        """
        if not self.store_path.exists():
            return {}
        return self.load()


def get_credential_source(env_name: str, store: CredentialsStore | None = None) -> tuple[str, str]:
    """Determine where a credential comes from.

    Returns:
        (source, masked_value) where source is "env", "store", or "missing"
    """
    # Check environment first
    env_value = os.environ.get(env_name)
    if env_value:
        return "env", _mask_value(env_value)

    # Check store
    if store is None:
        store = CredentialsStore()

    try:
        stored = store.load()
        if env_name in stored:
            return "store", _mask_value(stored[env_name])
    except PermissionError:
        pass

    return "missing", ""


def _mask_value(value: str) -> str:
    """Mask a credential value. Never reveals any characters of the secret.

    Returns "set (len=N)" so the operator can confirm a value is present
    without any characters of the secret appearing in output or logs.
    """
    if not value:
        return "(empty)"
    return f"set (len={len(value)})"
