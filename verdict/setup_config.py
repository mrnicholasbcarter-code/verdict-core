"""Shared setup configuration writer; preserve existing fields and backup first."""
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def config_path() -> Path:
    return Path(os.getenv("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "verdict" / "verdict.yaml"


def save_setup_config(values: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = yaml.safe_load(path.read_text()) if path.exists() else {}
    if existing is not None and not isinstance(existing, dict):
        raise ValueError("Existing configuration is not a mapping; refusing to overwrite.")
    merged = {**(existing or {}), **values}
    if path.exists():
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="verdict.yaml.backup-", delete=False) as backup:
            os.fchmod(backup.fileno(), 0o600)
            backup.write(path.read_bytes())
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".verdict-", delete=False) as pending:
        temporary = Path(pending.name)
        os.fchmod(pending.fileno(), 0o600)
        yaml.safe_dump(merged, pending, default_flow_style=False)
        pending.flush()
        os.fsync(pending.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
