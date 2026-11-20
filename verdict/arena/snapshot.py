"""Arena snapshot: the resolved store-id-keyed Arena data written to disk.

Schema:
{
  "schema_version": "1",
  "fetched_at": {"text": "ISO", "webdev": "ISO", "agent": "ISO"},
  "leaderboard_publish_date": {"text": "YYYY-MM-DD", ...},
  "reference_sets": {"planner": ["id1",...], "implement": [...], "reviewer": [...]},
  "records": {
    "<store_id>": {
      "arena_text":  {"default": {"raw":..., "lo":..., "hi":..., "n":..., "published":...,
                          "via":...}},
      "arena_webdev": {...},
      "arena_agent":  {...}
    }
  }
}
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ArenaSlot:
    """One Arena metric value for one (store_id, config, effort) combination."""

    raw: float
    lo: float
    hi: float
    n: float  # vote_count or session_count
    published: str  # leaderboard_publish_date
    via: str  # "canonical" | "explicit"

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "lo": self.lo,
            "hi": self.hi,
            "n": self.n,
            "published": self.published,
            "via": self.via,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ArenaSlot:
        return cls(
            raw=float(d["raw"]),
            lo=float(d["lo"]),
            hi=float(d["hi"]),
            n=float(d["n"]),
            published=str(d["published"]),
            via=str(d["via"]),
        )


# Config names (Arena leaderboard configs)
CONFIG_TEXT = "arena_text"
CONFIG_WEBDEV = "arena_webdev"
CONFIG_AGENT = "arena_agent"
ALL_CONFIGS = (CONFIG_TEXT, CONFIG_WEBDEV, CONFIG_AGENT)

# Mapping from Arena _config field to snapshot config name
ARENA_CONFIG_MAP = {"text": CONFIG_TEXT, "webdev": CONFIG_WEBDEV, "agent": CONFIG_AGENT}

# Floors per config (minimum n for a row to be usable)
FLOOR_N: dict[str, float] = {
    CONFIG_TEXT: 5000.0,
    CONFIG_WEBDEV: 1500.0,
    CONFIG_AGENT: 5000.0,  # session_count floor
}

STALENESS_DAYS = 45  # days before leaderboard_publish_date makes source stale


@dataclass
class ArenaSnapshot:
    """Resolved Arena → store_id snapshot, ready for VR calculation."""

    schema_version: str
    fetched_at: dict[str, str]  # config_name → ISO timestamp
    leaderboard_publish_date: dict[str, str]  # config_name → YYYY-MM-DD
    reference_sets: dict[str, list[str]]  # role → sorted store_ids
    # records[store_id][config_name][effort] = ArenaSlot
    records: dict[str, dict[str, dict[str, ArenaSlot]]]
    # coverage_summary[config_name] = {matched, dropped, total}
    coverage_summary: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fetched_at": self.fetched_at,
            "leaderboard_publish_date": self.leaderboard_publish_date,
            "reference_sets": self.reference_sets,
            "records": {
                sid: {
                    cfg: {slot: s.to_dict() for slot, s in slots.items()}
                    for cfg, slots in configs.items()
                }
                for sid, configs in self.records.items()
            },
            "coverage_summary": self.coverage_summary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ArenaSnapshot:
        records: dict[str, dict[str, dict[str, ArenaSlot]]] = {}
        for sid, configs in d.get("records", {}).items():
            records[sid] = {}
            for cfg, slots in configs.items():
                records[sid][cfg] = {slot: ArenaSlot.from_dict(s) for slot, s in slots.items()}
        return cls(
            schema_version=str(d.get("schema_version", "1")),
            fetched_at=dict(d.get("fetched_at", {})),
            leaderboard_publish_date=dict(d.get("leaderboard_publish_date", {})),
            reference_sets={k: list(v) for k, v in d.get("reference_sets", {}).items()},
            records=records,
            coverage_summary={k: dict(v) for k, v in d.get("coverage_summary", {}).items()},
        )


def default_snapshot_path() -> Path:
    from verdict.paths import verdict_home

    return verdict_home() / "arena-snapshot.json"


def load_snapshot(path: Path | None = None) -> ArenaSnapshot | None:
    """Load the Arena snapshot from disk; return None if missing or corrupt."""
    p = path or default_snapshot_path()
    try:
        with p.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return ArenaSnapshot.from_dict(data)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return None


def save_snapshot(snapshot: ArenaSnapshot, path: Path | None = None) -> None:
    """Atomically write the snapshot to disk."""
    p = path or default_snapshot_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False)
    # Atomic write via temp file + rename
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".arena-snapshot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
