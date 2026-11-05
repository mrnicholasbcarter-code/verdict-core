"""Secure credential storage for verdict.

Stores credentials in $XDG_CONFIG_HOME/verdict/credentials.env with:
- Directory permissions 0700
- File permissions 0600
- Atomic writes via temp file
- Refuses to load when group/world readable
- Never imports at module level; load explicitly in CLI/server entry
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path

#: Environment names that carry the OmniRoute gateway key, highest first.
GATEWAY_KEY_ENV_NAMES = ("VERDICT_OMNIROUTE_API_KEY", "OMNIROUTE_API_KEY")
#: Owner-only key file next to the credential store (written by the operator).
GATEWAY_KEY_FILENAME = "omniroute-api-key"
MANAGEMENT_TOKEN_ENV_NAME = "OMNIROUTE_MANAGEMENT_TOKEN"
#: Names ``CredentialsStore.load_into_env`` copied into this process's environment,
#: mapped to a SHA-256 digest of the copied value (never the value itself).
_STORE_INJECTED: dict[str, str] = {}


def _value_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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

        Exported env vars always win over stored values. Each name it sets is
        recorded (with a digest of the value, never the value) so provenance
        stays "credential store" for this process; see :func:`injected_from_store`.
        The record covers the latest call only: it is reset first (even if the
        load then fails), so a name this call does not inject is dropped.
        """
        _STORE_INJECTED.clear()
        stored = self.load()
        for key, value in stored.items():
            if key not in os.environ:
                os.environ[key] = value
                _STORE_INJECTED[key] = _value_digest(value)

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


def _owner_only_text(path: Path) -> str | None:
    """Stripped file text, or None when absent, unreadable or group/world accessible."""
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            return None
        return path.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _gateway_key_candidates(environ: Mapping[str, str]) -> Iterator[tuple[str, str]]:
    for name in GATEWAY_KEY_ENV_NAMES:
        value = (environ.get(name) or "").strip()
        if value:
            yield value, f"env:{name}"
    store = CredentialsStore()
    try:
        stored = store.load()
    except (PermissionError, OSError):
        stored = {}
    for name in GATEWAY_KEY_ENV_NAMES:
        value = stored.get(name, "").strip()
        if value:
            yield value, f"credential_store:{name}"
    key_file = _owner_only_text(store.config_dir / GATEWAY_KEY_FILENAME)
    if key_file:
        yield key_file, "key_file"
    from verdict.orchestration.prime_settings import default_prime_agent_dir

    auth_text = _owner_only_text(default_prime_agent_dir(environ) / "auth.json")
    try:
        auth = json.loads(auth_text) if auth_text else None
    except json.JSONDecodeError:
        auth = None
    entry = auth.get("omniroute") if isinstance(auth, dict) else None
    key = entry.get("key") if isinstance(entry, dict) else None
    if isinstance(key, str) and key.strip():
        yield key.strip(), "prime_auth"


def resolve_gateway_key(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """The OmniRoute gateway key and the name of its source, or None.

    Precedence: ``VERDICT_OMNIROUTE_API_KEY`` then ``OMNIROUTE_API_KEY`` from the
    environment, the same names in ``credentials.env``, the owner-only
    ``omniroute-api-key`` file in the same directory, then the ``omniroute`` key
    in Prime's ``auth.json``. Files readable by group or others are skipped.
    ``environ={}`` answers what a systemd user unit (no exported key) resolves.
    """
    source = os.environ if environ is None else environ
    return next(_gateway_key_candidates(source), None)


def resolve_gateway_binding(
    *, base_url: str | None = None, environ: Mapping[str, str] | None = None
) -> tuple[str, str | None]:
    """Resolve the canonical OpenAI-compatible gateway URL and its credential.

    An explicit URL wins over ``OMNIROUTE_BASE_URL``. The built-in gateway is
    localhost:20128, matching the supported OmniRoute service. The key follows
    :func:`resolve_gateway_key` precedence and is returned only to trusted
    callers that need to bind a transport or authenticated probe.
    """
    source = os.environ if environ is None else environ
    configured_url = (base_url or "").strip() or (source.get("OMNIROUTE_BASE_URL") or "").strip()
    url = configured_url or "http://127.0.0.1:20128"
    url = url.rstrip("/")
    if not url.endswith("/v1"):
        url = f"{url}/v1"
    resolved = resolve_gateway_key(source)
    return url, resolved[0] if resolved else None


def resolve_api_key() -> str | None:
    """The gateway key for this process; never logged, never persisted by Verdict."""
    found = resolve_gateway_key()
    return found[0] if found else None


def resolve_management_token(environ: Mapping[str, str] | None = None) -> str | None:
    """Resolve only the explicitly named management token from env or secure store."""
    source = os.environ if environ is None else environ
    value = (source.get(MANAGEMENT_TOKEN_ENV_NAME) or "").strip()
    if value:
        return value
    try:
        stored = CredentialsStore().load()
    except (PermissionError, OSError):
        return None
    value = (stored.get(MANAGEMENT_TOKEN_ENV_NAME) or "").strip()
    return value or None


def injected_from_store(name: str, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``name``'s environment value is the one ``load_into_env`` copied in.

    A value the process set or overwrote itself after the copy is not injected.
    """
    source = os.environ if environ is None else environ
    value = source.get(name)
    return value is not None and _STORE_INJECTED.get(name) == _value_digest(value)


def resolve_management_token_source(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """The management token and its provenance: ``environment`` or ``credential_store``.

    The token is exactly :func:`resolve_management_token`'s. Provenance is recorded,
    never inferred from values: a non-blank environment value is ``environment``
    unless ``load_into_env`` injected it from the store. An exported value equal to
    the stored one is still ``environment`` (it overrides the store).
    """
    source = os.environ if environ is None else environ
    token = resolve_management_token(source)
    if token is None:
        return None
    exported = bool((source.get(MANAGEMENT_TOKEN_ENV_NAME) or "").strip())
    if exported and not injected_from_store(MANAGEMENT_TOKEN_ENV_NAME, source):
        return token, "environment"
    return token, "credential_store"


def get_credential_source(env_name: str, store: CredentialsStore | None = None) -> tuple[str, str]:
    """Determine where a credential comes from.

    Returns:
        (source, masked_value) where source is "env", "store", or "missing";
        a gateway key name may also come from "key_file" or "prime_auth"
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

    if env_name in GATEWAY_KEY_ENV_NAMES:
        for value, kind in _gateway_key_candidates({}):
            if kind in {"key_file", "prime_auth"}:
                return kind, _mask_value(value)

    return "missing", ""


def _mask_value(value: str) -> str:
    """Mask a credential value. Never reveals any characters of the secret.

    Returns "set (len=N)" so the operator can confirm a value is present
    without any characters of the secret appearing in output or logs.
    """
    if not value:
        return "(empty)"
    return f"set (len={len(value)})"
