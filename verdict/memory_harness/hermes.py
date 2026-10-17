"""Hermes memory-bridge installer.

Matches the former ``hermes`` branch of ``configure_memory_bridge``: write
``verdict_bridge.json`` only when ``~/.hermes`` already exists, and overwrite
that file. The directory is not created. The caller still records ``hermes``
as configured when the directory is missing.
"""

from __future__ import annotations

import json
from pathlib import Path


def install_hermes_bridge(home: Path, shared_db: Path) -> None:
    """Write ``~/.hermes/verdict_bridge.json`` when the Hermes directory exists."""
    hermes_dir = home / ".hermes"
    if hermes_dir.exists():
        h_cfg = hermes_dir / "verdict_bridge.json"
        h_cfg.write_text(json.dumps({"memory_plane": str(shared_db)}), encoding="utf-8")
