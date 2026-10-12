"""Verdict state directory resolution.

Stdlib only: low layers and the cheap ``import verdict`` path import this, so it
must not import verdict modules or third-party packages.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def verdict_home(env: Mapping[str, str] | None = None) -> Path:
    """``$VERDICT_HOME``, else ``$HOME/.verdict``.

    Values are stripped and a blank one counts as unset, so an empty
    ``VERDICT_HOME`` never resolves to the working directory. ``env`` replaces
    ``os.environ`` (tests); a leading ``~`` expands to that mapping's ``HOME``.
    """
    source = os.environ if env is None else env
    home = (source.get("HOME") or "").strip()
    home_dir = Path(home) if home else Path.home()
    configured = (source.get("VERDICT_HOME") or "").strip()
    if not configured:
        return home_dir / ".verdict"
    path = Path(configured)
    if path.parts[:1] == ("~",):
        return home_dir.joinpath(*path.parts[1:])
    return path.expanduser()
