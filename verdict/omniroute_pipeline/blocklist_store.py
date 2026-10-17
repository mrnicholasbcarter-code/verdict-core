"""Persisted FAIL_FAKE provider blocklist (spec section 2: a hard billing DROP).

A provider whose probe trips the fake-200 guard is DROPped and stays
blocklisted until the controller clears it or the account changes; the
census never restores an entry on its own. Entries persist across ticks
in the pipeline's atomic 0600 store beside the other census evidence.
"""

from __future__ import annotations

import json
from pathlib import Path

from verdict.omniroute_pipeline.free_rule import _atomic_write
from verdict.omniroute_pipeline.state import ProviderBlocklist

BLOCKLIST_NAME = "provider-blocklist.json"


def default_blocklist_path() -> Path:
    from verdict.catalog_cache import default_catalog_cache_path

    return default_catalog_cache_path().parent / BLOCKLIST_NAME


def load_provider_blocklist(path: Path) -> ProviderBlocklist:
    """Persisted DROP entries; a missing or malformed file means none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ProviderBlocklist()
    if not isinstance(data, dict):
        return ProviderBlocklist()
    return ProviderBlocklist(
        entries={
            str(key): str(reason) for key, reason in sorted(data.items()) if isinstance(reason, str)
        }
    )


def save_provider_blocklist(path: Path, blocklist: ProviderBlocklist) -> None:
    """Atomic 0600 write of the current DROP entries (controller-cleared only)."""
    _atomic_write(path, json.dumps(dict(sorted(blocklist.entries.items())), sort_keys=True))
