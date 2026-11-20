"""Arena crosswalk loader and linter.

The crosswalk maps (arena_org, arena_key) → models_dev_ids (or explicit drop).
It is consulted before the canonical join to handle unusual spellings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CROSSWALK_PATH = Path(__file__).parent.parent / "data" / "arena-crosswalk.json"


@dataclass(frozen=True)
class CrosswalkMapEntry:
    arena_org: str
    arena_key: str
    arena_name: str
    models_dev_ids: tuple[str, ...]
    source: str
    verified: str


@dataclass(frozen=True)
class CrosswalkDropEntry:
    arena_org: str
    arena_key: str
    arena_name: str
    reason: str


CrosswalkEntry = CrosswalkMapEntry | CrosswalkDropEntry


@dataclass(frozen=True)
class Crosswalk:
    schema_version: str
    org_providers: dict[str, list[str]]
    map_entries: dict[tuple[str, str], CrosswalkMapEntry]  # (arena_org, arena_key) → entry
    drop_entries: dict[tuple[str, str], CrosswalkDropEntry]
    # entries in input order (for lint unused check)
    all_keys: tuple[tuple[str, str], ...]


def load_crosswalk(path: Path | None = None) -> Crosswalk:
    """Load the crosswalk JSON from disk."""
    p = path or DEFAULT_CROSSWALK_PATH
    with p.open(encoding="utf-8") as fh:
        data = json.load(fh)

    org_providers: dict[str, list[str]] = data.get("org_providers", {})
    map_entries: dict[tuple[str, str], CrosswalkMapEntry] = {}
    drop_entries: dict[tuple[str, str], CrosswalkDropEntry] = {}
    all_keys: list[tuple[str, str]] = []

    for entry in data.get("entries", []):
        arena_org: str = entry["arena_org"]
        arena_key: str = entry["arena_key"]
        arena_name: str = entry.get("arena_name", "")
        k = (arena_org, arena_key)
        all_keys.append(k)
        action = entry.get("action", "map")
        if action == "drop":
            drop_entries[k] = CrosswalkDropEntry(
                arena_org=arena_org,
                arena_key=arena_key,
                arena_name=arena_name,
                reason=entry.get("reason", ""),
            )
        else:
            if "source" not in entry or "verified" not in entry:
                raise ValueError(
                    f"crosswalk entry ({arena_org}, {arena_key}) missing source or verified"
                )
            map_entries[k] = CrosswalkMapEntry(
                arena_org=arena_org,
                arena_key=arena_key,
                arena_name=arena_name,
                models_dev_ids=tuple(entry["models_dev_ids"]),
                source=entry["source"],
                verified=entry["verified"],
            )

    return Crosswalk(
        schema_version=str(data.get("schema_version", "1")),
        org_providers=org_providers,
        map_entries=map_entries,
        drop_entries=drop_entries,
        all_keys=tuple(all_keys),
    )


@dataclass
class LintIssue:
    kind: str  # crosswalk_target_missing | crosswalk_unused
    key: tuple[str, str]
    detail: str


def lint_crosswalk(
    crosswalk: Crosswalk,
    arena_row_keys: set[tuple[str, str]],  # (org, key) for all Arena rows after canonicalisation
    store_ids: frozenset[str],
) -> list[LintIssue]:
    """Check crosswalk for missing targets and unused entries."""
    issues: list[LintIssue] = []

    # Check all map entries: target ids must exist in the store
    for k, entry in crosswalk.map_entries.items():
        for mid in entry.models_dev_ids:
            if mid not in store_ids:
                issues.append(
                    LintIssue(
                        kind="crosswalk_target_missing", key=k, detail=f"{mid!r} not found in store"
                    )
                )

    # Check for unused entries (match no Arena rows)
    for k in crosswalk.all_keys:
        if k not in arena_row_keys:
            issues.append(
                LintIssue(
                    kind="crosswalk_unused",
                    key=k,
                    detail=f"no Arena row matched ({k[0]!r}, {k[1]!r})",
                )
            )

    return issues
