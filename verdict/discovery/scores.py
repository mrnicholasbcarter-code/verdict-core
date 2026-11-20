"""verdict.discovery.scores — coding-quality scores from dated, attributable sources.

Ported from freescan/scores.py (D1, BOD-342).

No name-pattern tiers and no hand-typed numbers: every Score says what it is,
where it came from and its date.  A route with no trustworthy match gets None.

Sources, in priority order (first basis that matches wins; bases are never
blended into one number):

1. ``aa_coding_index``       Artificial Analysis coding index as carried by
                             OpenRouter's public model catalog (no key needed).
2. ``fcm_coding_index``      ``codingIndex`` from the free-coding-models
                             benchmark file (models.dev + curated; dated per entry).
3. ``aa_intelligence_index`` Artificial Analysis general intelligence index
                             (same OpenRouter catalog). Not a coding score; used
                             only when neither coding basis exists.

All three are already on a 0-100 scale, higher is better.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from verdict.discovery.model import Route, Score

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
FCM_URL = (
    "https://raw.githubusercontent.com/vava-nessa/free-coding-models/main/src/data/benchmarks.json"
)
BASIS_PRIORITY = ("aa_coding_index", "fcm_coding_index", "aa_intelligence_index")

Fetch = Callable[[str, float], object]

_DATE_SUFFIX = re.compile(r"-(?:20\d{6}|\d{6}|\d{4})$")
_WORD_SUFFIX = re.compile(r"-(?:instruct|it|latest|chat)$")


def normalise(model_id: str) -> str:
    """Reduce a provider-specific model id to a comparable name.

    Lowercase; drop the ``:variant`` suffix and any ``vendor/`` prefix; treat
    ``_`` and spaces as ``-`` (``3_3`` between digits becomes ``3.3``); drop
    trailing date stamps and a few serving words.  Family and version are kept,
    so two ids only compare equal when both agree.
    """
    s = model_id.strip().lower()
    s = s.split(":", 1)[0]
    s = s.rsplit("/", 1)[-1]
    s = re.sub(r"(?<=\d)_(?=\d)", ".", s)
    s = re.sub(r"[_\s]+", "-", s)
    for _ in range(3):
        new = _WORD_SUFFIX.sub("", _DATE_SUFFIX.sub("", s))
        if new == s:
            break
        s = new
    return s


def _has_version(name: str) -> bool:
    """A bare alias such as ``deepseek`` or ``kimi`` names no version."""
    return any(ch.isdigit() for ch in name)


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 <= float(value) <= 100 else None


def parse_openrouter(payload: object, as_of: str) -> list[tuple[str, Score]]:
    """Parse OpenRouter model catalog into (model_id, Score) pairs."""
    out: list[tuple[str, Score]] = []
    rows = payload.get("data") if isinstance(payload, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        bench = row.get("benchmarks")
        aa = bench.get("artificial_analysis") if isinstance(bench, dict) else None
        if not isinstance(aa, dict):
            continue
        for field, basis in (
            ("coding_index", "aa_coding_index"),
            ("intelligence_index", "aa_intelligence_index"),
        ):
            value = _num(aa.get(field))
            if value is not None:
                out.append((row["id"], Score(value, basis, OPENROUTER_URL, as_of, row["id"])))
    return out


def parse_fcm(payload: object, as_of: str) -> list[tuple[str, Score]]:
    """Parse free-coding-models benchmark JSON into (model_id, Score) pairs."""
    out: list[tuple[str, Score]] = []
    if not isinstance(payload, dict):
        return out
    for model_id, entry in payload.items():
        if model_id.startswith("_") or not isinstance(entry, dict):
            continue
        value = _num(entry.get("codingIndex"))
        if value is None:
            continue
        dated = entry.get("lastUpdated") if isinstance(entry.get("lastUpdated"), str) else as_of
        out.append((model_id, Score(value, "fcm_coding_index", FCM_URL, str(dated), model_id)))
    return out


SOURCES: tuple[tuple[str, str, Any], ...] = (
    ("openrouter_aa", OPENROUTER_URL, parse_openrouter),
    ("fcm_benchmarks", FCM_URL, parse_fcm),
)


class ScoreIndex:
    """Searchable index of coding scores from multiple sources."""

    def __init__(self, entries: list[tuple[str, Score]], sources: list[dict[str, object]]) -> None:
        self._sources = sources
        self._exact: dict[tuple[str, str], Score] = {}
        self._norm: dict[tuple[str, str], Score | None] = {}
        for source_id, score in entries:
            self._exact.setdefault((score.basis, source_id.lower()), score)
            key = (score.basis, normalise(source_id))
            if key not in self._norm:
                self._norm[key] = score
            else:
                kept = self._norm[key]
                # Two source ids that normalise alike but disagree on the
                # number are ambiguous: refuse the normalised match.
                if kept is not None and abs(kept.value - score.value) > 1e-9:
                    self._norm[key] = None

    @property
    def sources(self) -> list[dict[str, object]]:
        return list(self._sources)

    def lookup(self, route: Route) -> Score | None:
        """Return the best Score for a route, or None if no trustworthy match."""
        raw = route.model.strip().lower()
        candidates = [raw]
        if route.upstream and "/" not in raw:
            candidates.append(f"{route.upstream.strip().lower()}/{raw}")
        name = normalise(route.model)
        for basis in BASIS_PRIORITY:
            for cand in candidates:
                hit = self._exact.get((basis, cand))
                if hit:
                    return hit
            if not _has_version(name):
                continue
            hit = self._norm.get((basis, name))
            if hit:
                return Score(hit.value, hit.basis, hit.source, hit.as_of, hit.matched_id, False)
        return None


def _http_fetch(url: str, timeout: float) -> object:
    req = urllib.request.Request(
        url, headers={"User-Agent": "verdict/0.1", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _load_source(
    name: str,
    url: str,
    parse: Any,
    cache_dir: str,
    max_age_s: int,
    offline: bool,
    timeout: float,
    fetch: Fetch,
    now: float,
) -> tuple[list[tuple[str, Score]], dict[str, object]]:
    path = os.path.join(cache_dir, f"{name}.json")
    cached: dict[str, object] | None = None
    try:
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        cached = None
    status: dict[str, object] = {
        "name": name,
        "url": url,
        "as_of": None,
        "entries": 0,
        "ok": False,
        "error": None,
    }
    payload: object = None
    fetched_at: float | None = None
    cached_fetched_at: float | None = None
    if cached is not None:
        raw_ft = cached.get("fetched_at")
        cached_fetched_at = float(raw_ft) if isinstance(raw_ft, (int, float)) else None
    fresh = cached_fetched_at is not None and (now - cached_fetched_at) < max_age_s
    if fresh or (offline and cached is not None):
        payload = cached.get("payload") if cached else None
        fetched_at = cached_fetched_at
    elif not offline:
        try:
            payload, fetched_at = fetch(url, timeout), now
            try:
                os.makedirs(cache_dir, exist_ok=True)
                tmp = f"{path}.{os.getpid()}.tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"fetched_at": now, "payload": payload}, fh)
                os.replace(tmp, path)
            except OSError:
                pass
        except Exception as exc:
            status["error"] = f"{type(exc).__name__}: {exc}"[:200]
            if cached is not None:
                payload = cached.get("payload")
                fetched_at = cached_fetched_at
                status["error"] = str(status["error"]) + " (using stale cache)"
    else:
        status["error"] = "offline and no cache"
    if payload is None or fetched_at is None:
        return [], status
    as_of = time.strftime("%Y-%m-%d", time.gmtime(fetched_at))
    try:
        entries: list[tuple[str, Score]] = parse(payload, as_of)
    except Exception as exc:
        status["error"] = f"parse failed: {type(exc).__name__}: {exc}"[:200]
        return [], status
    status.update(as_of=as_of, entries=len(entries), ok=bool(entries))
    if not entries and not status["error"]:
        status["error"] = "source returned no usable scores"
    return entries, status


def load_scores(
    *,
    cache_dir: str = "~/.cache/verdict/discovery",
    max_age_s: int = 43200,
    offline: bool = False,
    timeout: float = 10.0,
    fetch: Fetch | None = None,
) -> ScoreIndex:
    """Fetch (or reuse cached) score sources.  Never raises."""
    expanded = os.path.expanduser(cache_dir)
    _fetch = fetch or _http_fetch
    now = time.time()
    entries: list[tuple[str, Score]] = []
    statuses: list[dict[str, object]] = []
    try:
        with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            jobs = [
                pool.submit(
                    _load_source, n, u, p, expanded, max_age_s, offline, timeout, _fetch, now
                )
                for n, u, p in SOURCES
            ]
            for job in jobs:
                got, status = job.result()
                entries.extend(got)
                statuses.append(status)
    except Exception as exc:
        statuses.append({"name": "load_scores", "ok": False, "error": repr(exc)[:200]})
    return ScoreIndex(entries, statuses)
