"""Cheap-path pack_state classifier (BOD-106).

Receipt-facing states:

* ``empty`` — zero included workspace units, even if omissions or a pack_digest exist
* ``partial`` — some includes, but a required high-value class that exists on disk
  is incomplete (ADR or architecture gathered / budget-omitted but not packed)
* ``hydrated`` — at least one included unit from each required high-value class
  that exists on disk (ADR + architecture when present; README is optional)
* ``failed`` — hydrate / compiler error path, or the task instructions themselves
  were omitted (BOD-110: a pack without its task is never hydrated or partial)

Task-required sources (ADR / architecture files matching the task terms) must
all be packed; a task-relevant ADR left out for budget is ``partial`` even if
another, smaller ADR made it in (BOD-110).

Invent-never: a missing root is a named ``source_missing`` omission only. It does
not count as a present high-value class and must not be invented to reach
``hydrated``.

Savings claims stay blocked until ``pack_state=hydrated`` with real
``included_sources``. Empty/partial plus a pretty ``pack_digest`` is still a
FAIL for rich hydrate. This classifier does not change admit / passport /
chooser hard gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from verdict.context_hydrate import (
    HYDRATE_CLASS_ADR,
    HYDRATE_CLASS_ARCHITECTURE,
    hydrate_priority_class,
)

PackState = Literal["empty", "partial", "hydrated", "failed"]
PACK_STATES: frozenset[str] = frozenset({"empty", "partial", "hydrated", "failed"})
REQUIRED_HIGH_VALUE_CLASSES: frozenset[int] = frozenset(
    {HYDRATE_CLASS_ADR, HYDRATE_CLASS_ARCHITECTURE}
)
_PRESENT_OMISSION_REASONS: frozenset[str] = frozenset(
    {"input_budget_exhausted", "unreadable", "unit_cap_exceeded"}
)


def classify_pack_state(
    *,
    included: Sequence[object] = (),
    gathered: Sequence[object] = (),
    omissions: Sequence[object] = (),
    failed: bool = False,
    required: Sequence[str] = (),
    task_complete: bool = True,
) -> PackState:
    """Classify a compiled cheap-path pack for admit/execute receipts.

    ``included`` / ``gathered`` items are provenance rows (``source_uri``).
    ``omissions`` items are named drops (``name`` + ``reason``).
    ``required`` names task-specific sources that must be packed; omitting any
    of them is ``partial`` even when the class-level thesis set landed (BOD-110).
    ``task_complete=False`` means the task instructions themselves did not
    survive compilation, which is ``failed`` — never hydrated, never partial.
    """
    if failed or not task_complete:
        return "failed"
    included_uris = tuple(uri for uri in (_item_uri(item) for item in included) if uri)
    if not included_uris:
        return "empty"
    present = _present_required_classes(gathered=gathered, omissions=omissions)
    packed = {_required_class(uri) for uri in included_uris}
    packed.discard(None)
    if present - packed:
        return "partial"
    if any(uri.strip() and uri.strip() not in included_uris for uri in required):
        return "partial"
    return "hydrated"


def savings_unlocked(pack_state: str | None) -> bool:
    """Return whether cheap-path savings may be claimed.

    Product/QA lock: savings stay blocked until ``pack_state=hydrated``.
    """
    return pack_state == "hydrated"


def _present_required_classes(
    *, gathered: Sequence[object], omissions: Sequence[object]
) -> set[int]:
    present: set[int] = set()
    for item in gathered:
        cls = _required_class(_item_uri(item))
        if cls is not None:
            present.add(cls)
    for item in omissions:
        if _item_reason(item) not in _PRESENT_OMISSION_REASONS:
            continue
        cls = _required_class(_item_uri(item) or _item_name(item))
        if cls is not None:
            present.add(cls)
    return present


def _required_class(uri: str) -> int | None:
    if not uri or uri.startswith("urn:"):
        return None
    cls = hydrate_priority_class(Path(uri))
    if cls in REQUIRED_HIGH_VALUE_CLASSES:
        return cls
    return None


def _item_uri(item: object) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        raw = item.get("source_uri") or item.get("name") or ""
        return str(raw).strip()
    raw = getattr(item, "source_uri", None) or getattr(item, "name", None) or ""
    return str(raw).strip()


def _item_name(item: object) -> str:
    if isinstance(item, Mapping):
        return str(item.get("name") or "").strip()
    return str(getattr(item, "name", "") or "").strip()


def _item_reason(item: object) -> str:
    if isinstance(item, Mapping):
        return str(item.get("reason") or "").strip()
    return str(getattr(item, "reason", "") or "").strip()


__all__ = [
    "PACK_STATES",
    "REQUIRED_HIGH_VALUE_CLASSES",
    "PackState",
    "classify_pack_state",
    "savings_unlocked",
]
