"""Bounded, auditable Prime model-visibility snapshots.

This module manages *visibility* only.  A gateway inventory or its LKG may say
which explicit ids Prime has been configured to show; it is never health,
capacity, entitlement, or launch authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.admission import is_opaque

SIDECAR_NAME = "omniroute-inventory.verdict.json"


@dataclass(frozen=True)
class PrimeInventorySnapshot:
    source: str
    refreshed_at: str
    route_ids: tuple[str, ...]
    count: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "refreshed_at": self.refreshed_at,
            "route_ids": list(self.route_ids),
            "count": self.count,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class PrimeInventoryFailure:
    source: str
    failed_at: str
    error_type: str

    def to_dict(self) -> dict[str, str]:
        return {"source": self.source, "failed_at": self.failed_at, "error_type": self.error_type}


@dataclass(frozen=True)
class PrimeInventoryStatus:
    snapshot: PrimeInventorySnapshot | None
    failure: PrimeInventoryFailure | None
    refreshed: bool
    fresh: bool
    excluded_ids: tuple[str, ...] = ()

    @property
    def source(self) -> str | None:
        return self.snapshot.source if self.snapshot else None

    @property
    def timestamp(self) -> str | None:
        return self.snapshot.refreshed_at if self.snapshot else None

    @property
    def count(self) -> int:
        return self.snapshot.count if self.snapshot else 0

    @property
    def digest(self) -> str | None:
        return self.snapshot.digest if self.snapshot else None


def sidecar_path(models_path: Path) -> Path:
    return models_path.parent / SIDECAR_NAME


def concrete_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[tuple[Mapping[str, Any], ...], tuple[str, ...]]:
    """Keep only explicit concrete route rows, in deterministic id order."""
    kept: dict[str, Mapping[str, Any]] = {}
    excluded: set[str] = set()
    for row in rows:
        raw = row.get("id")
        route_id = raw.strip() if isinstance(raw, str) else ""
        if not route_id:
            continue
        if is_opaque(route_id) or str(row.get("owned_by", "")).lower() == "combo":
            excluded.add(route_id)
            continue
        kept.setdefault(route_id, row)
    return tuple(kept[key] for key in sorted(kept)), tuple(sorted(excluded))


def _digest(route_ids: tuple[str, ...]) -> str:
    body = json.dumps({"route_ids": list(route_ids)}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()


def _parse_snapshot(value: Any) -> PrimeInventorySnapshot | None:
    if not isinstance(value, Mapping):
        return None
    ids = value.get("route_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) and item for item in ids):
        return None
    route_ids = tuple(sorted(set(ids)))
    source, refreshed_at, digest = value.get("source"), value.get("refreshed_at"), value.get("digest")
    if not all(isinstance(item, str) and item for item in (source, refreshed_at, digest)):
        return None
    assert isinstance(source, str) and isinstance(refreshed_at, str) and isinstance(digest, str)
    if value.get("count") != len(route_ids) or digest != _digest(route_ids):
        return None
    return PrimeInventorySnapshot(source, refreshed_at, route_ids, len(route_ids), digest)


def _parse_failure(value: Any) -> PrimeInventoryFailure | None:
    if not isinstance(value, Mapping):
        return None
    source, failed_at, error_type = value.get("source"), value.get("failed_at"), value.get("error_type")
    if not all(isinstance(item, str) and item for item in (source, failed_at, error_type)):
        return None
    assert isinstance(source, str) and isinstance(failed_at, str) and isinstance(error_type, str)
    return PrimeInventoryFailure(source, failed_at, error_type)


def _read(path: Path) -> tuple[PrimeInventorySnapshot | None, PrimeInventoryFailure | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    return _parse_snapshot(raw.get("last_success") if isinstance(raw, Mapping) else None), _parse_failure(raw.get("last_failure") if isinstance(raw, Mapping) else None)


def _write(path: Path, snapshot: PrimeInventorySnapshot | None, failure: PrimeInventoryFailure | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "verdict.prime-inventory/v1",
        "last_success": snapshot.to_dict() if snapshot else None,
        "last_failure": failure.to_dict() if failure else None,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _fresh(snapshot: PrimeInventorySnapshot | None, now: datetime, max_age_seconds: float) -> bool:
    if snapshot is None or max_age_seconds < 0:
        return False
    try:
        seen = datetime.fromisoformat(snapshot.refreshed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if seen.tzinfo is None:
        return False
    return now <= seen.astimezone(timezone.utc) + timedelta(seconds=max_age_seconds)


def refresh_prime_inventory(
    *,
    models_path: Path,
    source: str,
    fetch_rows: Callable[[], Sequence[Mapping[str, Any]]],
    apply_rows: Callable[[Sequence[Mapping[str, Any]]], Any],
    max_age_seconds: float = 300.0,
    force: bool = False,
    now: Callable[[], datetime] | None = None,
) -> PrimeInventoryStatus:
    """Refresh visibility before selection; cache reads themselves never perform I/O.

    `apply_rows` is intentionally separate so this module neither knows provider
    credentials nor grants any launch authority.
    """
    current = (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    state_path = sidecar_path(models_path)
    previous, previous_failure = _read(state_path)
    if not force and _fresh(previous, current, max_age_seconds):
        return PrimeInventoryStatus(previous, previous_failure, refreshed=False, fresh=True)
    try:
        rows, excluded = concrete_rows(fetch_rows())
        if not rows:
            raise ValueError("empty_concrete_inventory")
        apply_rows(rows)
    except Exception as exc:
        failure = PrimeInventoryFailure(source, current.isoformat(), type(exc).__name__)
        _write(state_path, previous, failure)
        return PrimeInventoryStatus(previous, failure, refreshed=False, fresh=False)
    ids = tuple(str(row["id"]).strip() for row in rows)
    snapshot = PrimeInventorySnapshot(source, current.isoformat(), ids, len(ids), _digest(ids))
    _write(state_path, snapshot, None)
    return PrimeInventoryStatus(snapshot, None, refreshed=True, fresh=True, excluded_ids=excluded)
