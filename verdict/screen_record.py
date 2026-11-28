"""What the scheduled screen's probes returned, per provider, over the last seven days.

The record decides which routes the screen skips (a provider that never passes) and
in what order it probes the rest. It never admits a route, marks one healthy or
grants a proof: a route is still eligible only through its own tool proof in the
health cache. Every value here comes from Verdict's own probe results, never from
text a provider sent.

Why a file and not the health cache: the cache keeps one entry per route and a
later failure resets that route's ``agentic_ok``, so a pass inside the window and
repeat probes of one route are lost. A provider with fewer than eight routes could
then never reach the back-off threshold.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

from verdict.orchestration.health_cache import CATEGORY_CATALOG_STALE, CATEGORY_NOT_FOUND

RECORD_FILE = "screen-record.json"
RECORD_SCHEMA_VERSION = 1

# The window every count is taken over.
RECORD_WINDOW = timedelta(days=7)
# A provider with at least this many counted results and no pass in the window is
# backed off.
BACKOFF_MIN_COUNTED_RESULTS = 8
# A backed-off provider is still given one probe per this period.
BACKOFF_PROBE_INTERVAL = timedelta(hours=24)
# While the third group of the queue is not empty, at least one probe in this many
# goes to it, so a provider with no record yet gets one.
THIRD_GROUP_PROBE_SHARE = 8
# The file never grows past this many results (newest kept).
RECORD_MAX_ENTRIES = 20_000

KIND_PASS = "pass"
KIND_TOOL_FAILURE = "tool_failure"
# The only results that count toward the back-off: a pass, a model that answered and
# did not use the tool, and the provider saying it does not serve the model. Anything
# else (a rate limit, a timeout, an upstream 5xx, a refusal by the gateway before any
# upstream call, an auth or billing refusal, an identity mismatch) says nothing about
# whether the provider's models can use tools, so it is recorded and not counted.
COUNTED_KINDS = frozenset(
    {KIND_PASS, KIND_TOOL_FAILURE, CATEGORY_CATALOG_STALE, CATEGORY_NOT_FOUND}
)
# Health-cache categories that a failed agentic probe leaves when the model answered.
_TOOL_FAILURE_CATEGORIES = frozenset({"agentic_fail", "ok"})

ERROR_UNREADABLE = "unreadable"
ERROR_ENTRIES_DROPPED = "invalid_entries_dropped"
ERROR_UNWRITABLE = "unwritable"

T = TypeVar("T")


def result_kind(result: str, category: str | None) -> str:
    """The kind of one probe that sent requests.

    ``category`` is the health-cache entry's category and is read only for a ``fail``:
    for every other result no entry was written, and the entry would be an older
    probe's.
    """
    if result == "pass":
        return KIND_PASS
    if result != "fail":
        return result
    if category in _TOOL_FAILURE_CATEGORIES:
        return KIND_TOOL_FAILURE
    return category or "fail"


@dataclass(frozen=True)
class ProbeRecord:
    provider: str
    route_id: str
    kind: str
    at: datetime


@dataclass(frozen=True)
class ProviderSummary:
    """One provider's results in the window."""

    provider: str
    passes: int
    counted: int
    probes: int
    by_kind: dict[str, int]
    last_probe_at: datetime | None
    backed_off: bool

    @property
    def pass_rate(self) -> float | None:
        """Passes over counted results; ``None`` when nothing counted (not zero)."""
        return self.passes / self.counted if self.counted else None


class ScreenRecord:
    """The window of probe results, loaded once per run and saved after each probe."""

    def __init__(
        self, path: Path, entries: Iterable[ProbeRecord] = (), *, error: str | None = None
    ) -> None:
        self.path = path
        self._entries: list[ProbeRecord] = list(entries)
        self.error = error

    @classmethod
    def load(cls, path: Path) -> ScreenRecord:
        """Read the file. A missing file is an empty record; an unreadable one is an
        empty record that says so, which leaves the screen probing as it did before."""
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path)
        except (OSError, UnicodeDecodeError):
            return cls(path, error=ERROR_UNREADABLE)
        try:
            payload = json.loads(raw)
        except ValueError:
            return cls(path, error=ERROR_UNREADABLE)
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != RECORD_SCHEMA_VERSION
            or not isinstance(payload.get("results"), list)
        ):
            return cls(path, error=ERROR_UNREADABLE)
        entries: list[ProbeRecord] = []
        dropped = False
        for item in payload["results"]:
            parsed = _parse_entry(item)
            if parsed is None:
                dropped = True
            else:
                entries.append(parsed)
        return cls(path, entries, error=ERROR_ENTRIES_DROPPED if dropped else None)

    # -- reads --------------------------------------------------------------

    def _in_window(self, now: datetime) -> list[ProbeRecord]:
        # Both ends: a result stamped after ``now`` (a clock that was put back) is not
        # evidence from the last seven days, and counting it could back a provider
        # off and hold its daily probe until that future time.
        floor = now - RECORD_WINDOW
        return [entry for entry in self._entries if floor <= entry.at <= now]

    def summary(self, provider: str, now: datetime) -> ProviderSummary:
        return _summarise(provider, [e for e in self._in_window(now) if e.provider == provider])

    def summaries(self, now: datetime) -> list[ProviderSummary]:
        by_provider: dict[str, list[ProbeRecord]] = {}
        for entry in self._in_window(now):
            by_provider.setdefault(entry.provider, []).append(entry)
        return [_summarise(name, by_provider[name]) for name in sorted(by_provider)]

    def probe_allowed(self, provider: str, now: datetime) -> bool:
        """False only for a backed-off provider that was probed less than a period ago."""
        summary = self.summary(provider, now)
        if not summary.backed_off or summary.last_probe_at is None:
            return True
        return now - summary.last_probe_at >= BACKOFF_PROBE_INTERVAL

    # -- writes -------------------------------------------------------------

    def add(self, provider: str, route_id: str, kind: str, at: datetime) -> None:
        self._entries.append(ProbeRecord(provider, route_id, kind, at))

    def save(self, now: datetime) -> None:
        """Write the window atomically. A failure sets ``error`` and raises nothing:
        the screen keeps its previous behaviour when it cannot remember."""
        kept = self._in_window(now)[-RECORD_MAX_ENTRIES:]
        self._entries = kept
        payload = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "results": [
                {
                    "provider": e.provider,
                    "route_id": e.route_id,
                    "kind": e.kind,
                    "at": e.at.isoformat(),
                }
                for e in kept
            ],
        }
        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.path.parent, prefix=".screen-record-", delete=False
            ) as pending:
                temporary = Path(pending.name)
                os.fchmod(pending.fileno(), 0o600)
                json.dump(payload, pending, sort_keys=True, allow_nan=False)
                pending.flush()
                os.fsync(pending.fileno())
            temporary.replace(self.path)
        except BaseException as exc:
            # Every exit path removes the temporary file; only a write failure is
            # absorbed, anything else (an interrupt, a bug) still propagates.
            if temporary is not None:
                # A cleanup failure must not replace the original error or stop the
                # screen: the file is only a leftover.
                with contextlib.suppress(OSError):
                    temporary.unlink(missing_ok=True)
            if not isinstance(exc, (OSError, ValueError)):
                raise
            self.error = ERROR_UNWRITABLE


def _parse_entry(item: Any) -> ProbeRecord | None:
    if not isinstance(item, dict):
        return None
    provider, route_id, kind, at = (item.get(k) for k in ("provider", "route_id", "kind", "at"))
    if not all(isinstance(v, str) and v for v in (provider, route_id, kind, at)):
        return None
    try:
        stamp = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return ProbeRecord(str(provider), str(route_id), str(kind), stamp)


def _summarise(provider: str, entries: Sequence[ProbeRecord]) -> ProviderSummary:
    by_kind = Counter(entry.kind for entry in entries)
    passes = by_kind[KIND_PASS]
    counted = sum(count for kind, count in by_kind.items() if kind in COUNTED_KINDS)
    return ProviderSummary(
        provider=provider,
        passes=passes,
        counted=counted,
        probes=len(entries),
        by_kind=dict(sorted(by_kind.items())),
        last_probe_at=max((e.at for e in entries), default=None),
        backed_off=passes == 0 and counted >= BACKOFF_MIN_COUNTED_RESULTS,
    )


def provider_record_report(record: ScreenRecord, now: datetime) -> dict[str, Any]:
    """The screen's JSON block: per provider, results by kind for the window.

    ``providers`` is ``null`` when the file could not be read and this run has added
    nothing: the history is unknown, which is not the same as no results.
    """
    summaries = record.summaries(now)
    return {
        "window_days": RECORD_WINDOW.days,
        "backoff_min_counted_results": BACKOFF_MIN_COUNTED_RESULTS,
        "backoff_probe_interval_hours": BACKOFF_PROBE_INTERVAL.total_seconds() / 3600,
        "counted_kinds": sorted(COUNTED_KINDS),
        "error": record.error,
        "providers": None
        if record.error == ERROR_UNREADABLE and not summaries
        else {
            s.provider: {
                "passes": s.passes,
                "counted_results": s.counted,
                "probes": s.probes,
                "by_kind": s.by_kind,
                "pass_rate": s.pass_rate,
                "backed_off": s.backed_off,
                "last_probe_at": s.last_probe_at.isoformat() if s.last_probe_at else None,
            }
            for s in summaries
        },
    }


def split_by_record(
    items: Sequence[T],
    *,
    route_id_of: Callable[[T], str],
    admitted: frozenset[str],
    provider_of: Callable[[str], str],
    record: ScreenRecord,
    now: datetime,
) -> tuple[list[T], list[T]]:
    """Order the candidates and split off the third group.

    Groups: routes the passive check admitted; other routes of a provider with a
    pass in the window; the rest. Inside a group the provider's pass rate comes first
    (no counted result sorts as zero) and the given order breaks ties. Returns the
    first two groups, in order, and the third.
    """
    summaries = {s.provider: s for s in record.summaries(now)}
    keyed: list[tuple[int, float, int, T]] = []
    for index, item in enumerate(items):
        route_id = route_id_of(item)
        summary = summaries.get(provider_of(route_id))
        if route_id in admitted:
            group = 1
        elif summary is not None and summary.passes > 0:
            group = 2
        else:
            group = 3
        keyed.append((group, -((summary.pass_rate if summary else None) or 0.0), index, item))
    keyed.sort(key=lambda k: k[:3])
    return [k[3] for k in keyed if k[0] != 3], [k[3] for k in keyed if k[0] == 3]
