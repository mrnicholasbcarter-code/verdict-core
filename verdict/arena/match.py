"""Arena → store-id exact join.

No fuzzy matching. The store index is accessed only via `.get()`.
Proof: the index class raises on any iteration attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from verdict.arena.canonical import (
    DROP_CROSSWALK_DROP,
    DROP_CROSSWALK_TARGET_MISSING,
    DROP_NO_STORE_RECORD,
    DROP_VARIANT_COLLISION,
    Drop,
    Parsed,
    parse_name,
    slug,
)
from verdict.arena.crosswalk import Crosswalk


@dataclass(frozen=True)
class Match:
    """A successfully matched Arena row."""

    model_name: str
    organization: str
    parsed: Parsed
    store_ids: tuple[str, ...]
    via: str  # "canonical" | "explicit"
    _config: str
    row: dict[str, Any]  # original Arena row (for coverage report)


class RestrictedIndex:
    """A dict wrapper that forbids iteration; only .get() is allowed.

    This is used in tests to prove no fuzzy matching occurs.
    """

    def __init__(self, mapping: dict[tuple[str, str], tuple[str, ...]]) -> None:
        self._data = mapping

    def get(
        self, key: tuple[str, str], default: tuple[str, ...] | None = None
    ) -> tuple[str, ...] | None:
        return self._data.get(key, default)

    def __iter__(self) -> NoReturn:
        raise RuntimeError("RestrictedIndex: iteration forbidden; use .get() only")

    def __len__(self) -> NoReturn:
        raise RuntimeError("RestrictedIndex: len() forbidden; use .get() only")

    def keys(self) -> NoReturn:
        raise RuntimeError("RestrictedIndex: .keys() forbidden; use .get() only")

    def values(self) -> NoReturn:
        raise RuntimeError("RestrictedIndex: .values() forbidden; use .get() only")

    def items(self) -> NoReturn:
        raise RuntimeError("RestrictedIndex: .items() forbidden; use .get() only")


def build_store_index(store_ids: frozenset[str]) -> RestrictedIndex:
    """Build (org, slug(key)) → sorted store_ids for all store model ids.

    E.g. 'anthropic/claude-opus-4-6' and 'anthropic/claude-opus-4.6' both
    canonicalise to ('anthropic', 'claude-opus-4-6') and are grouped together
    (twins).
    """
    index: dict[tuple[str, str], list[str]] = {}
    for sid in store_ids:
        if "/" not in sid:
            continue
        org, key_part = sid.split("/", 1)
        canonical_key = slug(key_part)
        if not canonical_key:
            continue
        idx_key = (org, canonical_key)
        index.setdefault(idx_key, []).append(sid)
    # Sort each bucket for determinism
    return RestrictedIndex({k: tuple(sorted(v)) for k, v in index.items()})


def match_rows(
    arena_rows: list[dict[str, Any]], store_index: RestrictedIndex, crosswalk: Crosswalk
) -> tuple[list[Match], list[Drop]]:
    """Match Arena rows against the store index.

    Returns (matches, drops). Collisions (two rows on the same (store_id, config, slot))
    are both dropped as variant_collision.
    """
    matches: list[Match] = []
    drops: list[Drop] = []
    # Track (store_id, config, slot) for collision detection — keyed per sid so twins are caught
    seen_per_sid: dict[tuple[str, str, str], str] = {}  # (sid, config, effort) → model_name

    for row in arena_rows:
        model_name: str = str(row.get("model_name", ""))
        organization: str = str(row.get("organization", ""))
        config: str = str(row.get("_config", ""))
        category: str = str(row.get("category", ""))
        if category != "overall":
            continue  # only 'overall' rows

        # Step 1: check explicit crosswalk (drop entries first)
        # Canonicalise to get (org, key) for crosswalk lookup
        parsed_result = parse_name(model_name, organization, crosswalk.org_providers)
        if isinstance(parsed_result, Drop):
            # Backfill config since parse_name doesn't know it yet
            drops.append(Drop(**{**parsed_result.__dict__, "config": config}))
            continue

        parsed = parsed_result
        cw_key = (parsed.org, parsed.key)

        # Check crosswalk drop entries
        if cw_key in crosswalk.drop_entries:
            drop_entry = crosswalk.drop_entries[cw_key]
            drops.append(
                Drop(
                    model_name=model_name,
                    organization=organization,
                    reason=DROP_CROSSWALK_DROP,
                    detail=drop_entry.reason,
                    config=config,
                )
            )
            continue

        # Check crosswalk map entries (explicit override)
        if cw_key in crosswalk.map_entries:
            map_entry = crosswalk.map_entries[cw_key]
            # Verify each explicit target is actually in the store index.
            valid_ids: list[str] = []
            for sid in map_entry.models_dev_ids:
                if "/" not in sid:
                    continue
                sid_org, sid_key = sid.split("/", 1)
                bucket = store_index.get((sid_org, slug(sid_key)))
                if bucket and sid in bucket:
                    valid_ids.append(sid)
            if not valid_ids:
                drops.append(
                    Drop(
                        model_name=model_name,
                        organization=organization,
                        reason=DROP_CROSSWALK_TARGET_MISSING,
                        detail=f"none of {list(map_entry.models_dev_ids)!r} found in store",
                        config=config,
                    )
                )
                continue
            store_ids_found = tuple(sorted(valid_ids))
            via = "explicit"
        else:
            # Canonical join: try all provider prefixes for this Arena org
            provider_prefixes = crosswalk.org_providers.get(parsed.org, [parsed.org])
            all_store_ids: list[str] = []
            for prefix in provider_prefixes:
                found = store_index.get((prefix, parsed.key))
                if found:
                    all_store_ids.extend(found)
            if not all_store_ids:
                drops.append(
                    Drop(
                        model_name=model_name,
                        organization=organization,
                        reason=DROP_NO_STORE_RECORD,
                        detail=f"no store id for ({parsed.org!r}, {parsed.key!r})",
                        config=config,
                    )
                )
                continue
            store_ids_found = tuple(sorted(set(all_store_ids)))
            via = "canonical"

        # Collision check: each (store_id, config, effort) can appear at most once.
        # Key per individual sid so twins (multiple sids sharing a canonical key) are caught.
        collision = False
        colliding_name: str | None = None
        for sid in store_ids_found:
            slot_key: tuple[str, str, str] = (sid, config, parsed.effort)
            if slot_key in seen_per_sid:
                colliding_name = seen_per_sid[slot_key]
                collision = True
                break

        if collision:
            assert colliding_name is not None
            # Drop the new duplicate
            drops.append(
                Drop(
                    model_name=model_name,
                    organization=organization,
                    reason=DROP_VARIANT_COLLISION,
                    detail=f"duplicate ({config!r}, {parsed.effort!r}) for {colliding_name!r}",
                    config=config,
                )
            )
            # Retroactively drop the first match (any overlap in store_ids for same config+effort)
            new_sids = set(store_ids_found)
            to_remove = [
                m
                for m in matches
                if bool(set(m.store_ids) & new_sids)
                and m._config == config
                and m.parsed.effort == parsed.effort
            ]
            # Clean up seen_per_sid for removed matches so later rows do not false-collide
            for rm in to_remove:
                for sid in rm.store_ids:
                    seen_per_sid.pop((sid, config, rm.parsed.effort), None)
            remove_set = set(id(m) for m in to_remove)
            matches = [m for m in matches if id(m) not in remove_set]
            for rm in to_remove:
                drops.append(
                    Drop(
                        model_name=rm.model_name,
                        organization=organization,
                        reason=DROP_VARIANT_COLLISION,
                        detail=f"duplicate ({config!r}, {parsed.effort!r}) for {model_name!r}",
                        config=config,
                    )
                )

        if not collision:
            for sid in store_ids_found:
                seen_per_sid[(sid, config, parsed.effort)] = model_name
            matches.append(
                Match(
                    model_name=model_name,
                    organization=organization,
                    parsed=parsed,
                    store_ids=store_ids_found,
                    via=via,
                    _config=config,
                    row=row,
                )
            )

    return matches, drops
