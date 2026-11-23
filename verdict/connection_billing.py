"""Connection billing catalog lookup for capacity classification.

Loads the pinned snapshot at ``verdict/data/omniroute_connection_billing.json``
and an optional operator overlay from the Verdict config directory.  Returns a
``BillingFact`` that the capacity classifier uses to decide ``SUBSCRIPTION``,
``METERED`` or ``FREE`` without relying on ``authType`` alone.

Lookup order (design.md Decision §2):
  1. Overlay entry matching ``(provider, authType)``          (most specific)
  2. Overlay entry matching ``(provider, None)``              (provider-wide overlay)
  3. Snapshot entry matching ``(provider, authType)``         (most specific)
  4. Snapshot entry matching ``(provider, None)``             (provider-wide snapshot)
  5. ``None`` — billing unknown; not a fact                   (falls through)

An ``authType``-specific entry wins over a provider-wide entry in both sources.
An overlay entry always wins over the snapshot for the same (provider, authType).

Billing ``"unknown"`` and a missing entry are not facts (ADR-049: absence is not
proof).  They are both represented by returning ``None`` from ``lookup``.

No imports from ``verdict.capacity`` or ``verdict.orchestration`` — this module
is imported by ``capacity.py``, so the dependency is one-way.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

BILLING_SUBSCRIPTION = "subscription"
BILLING_METERED = "metered"
BILLING_KEYLESS = "keyless"
BILLING_UNKNOWN = "unknown"

OVERAGE_HARD_STOP = "hard-stop"
OVERAGE_METERS_TO_PAID = "meters-to-paid"
OVERAGE_UNKNOWN = "unknown"

SOURCE_SNAPSHOT = "snapshot"
SOURCE_OVERLAY = "overlay"

# ---------------------------------------------------------------------------
# Provider aliases for billing catalog lookup
# ---------------------------------------------------------------------------

# Maps provider names that share a billing catalog entry.  ``agy`` and
# ``antigravity`` are the same Google Antigravity subscription pool
# (evidence: research-empirical.md); the OmniRoute billing catalog uses
# ``antigravity`` as the canonical key.  Callers that see ``agy`` get the
# same billing facts.
_BILLING_PROVIDER_ALIASES: dict[str, str] = {"agy": "antigravity"}


@dataclass(frozen=True)
class BillingFact:
    """A concrete billing fact for a (provider, authType) combination.

    ``billing`` is one of ``"subscription"``, ``"metered"``, ``"keyless"``.
    ``overage`` is one of ``"hard-stop"``, ``"meters-to-paid"``, ``"unknown"``.
    ``source`` is ``"snapshot"`` or ``"overlay"``.
    """

    billing: str
    overage: str
    source: str  # "snapshot" or "overlay"


# ---------------------------------------------------------------------------
# Snapshot loading (cached for the process lifetime)
# ---------------------------------------------------------------------------

_SNAPSHOT_PATH = Path(__file__).parent / "data" / "omniroute_connection_billing.json"


@lru_cache(maxsize=1)
def _load_snapshot() -> tuple[dict[tuple[str, str | None], BillingFact], dict[str, Any]]:
    """Load the pinned snapshot once.  Returns (index, meta)."""
    raw = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    meta: dict[str, Any] = raw.get("_meta", {})
    index: dict[tuple[str, str | None], BillingFact] = {}
    for entry in raw.get("entries", []):
        billing = str(entry.get("billing", BILLING_UNKNOWN))
        overage = str(entry.get("overage", OVERAGE_UNKNOWN))
        if billing == BILLING_UNKNOWN:
            continue  # unknown is not a fact — omit from index
        provider = str(entry["provider"]).lower()
        auth_type: str | None = entry.get("authType")
        if isinstance(auth_type, str):
            auth_type = auth_type.lower()
        key = (provider, auth_type)
        index[key] = BillingFact(billing=billing, overage=overage, source=SOURCE_SNAPSHOT)
    return index, meta


def snapshot_meta() -> dict[str, Any]:
    """Return the snapshot's ``_meta`` block (version, sha256, etc.)."""
    _, meta = _load_snapshot()
    return meta


# ---------------------------------------------------------------------------
# Overlay loading (mtime-cached so a fresh VERDICT_HOME in tests is visible)
# ---------------------------------------------------------------------------

# Re-stat the overlay file at most once per 100 ms within the same process.
# This eliminates per-call syscall overhead in tight classification loops (e.g.
# a 7000-row projection) while still picking up operator edits promptly.
_OVERLAY_STAT_TTL_NS = 100_000_000  # 100 ms in nanoseconds

# Cache tuple: (cache_key, mtime_ns, index, last_stat_ns)
# cache_key is verdict_home(env).as_posix() — a cheap path-string comparison
# that detects env changes without reading "VERDICT_HOME" directly (paths.py
# owns all env-var reads per test_no_other_module_reads_verdict_home_directly).
_overlay_cache: tuple[str, int, dict[tuple[str, str | None], BillingFact], int] | None = None

# Thread-local for batch callers (e.g. routing_view) that load the overlay once
# and share it across all lookup() calls within a single projection.  When
# _batch.overlay is set, lookup() uses it instead of calling _load_overlay().
_batch: threading.local = threading.local()


@contextmanager
def preload_overlay(env: Mapping[str, str] | None = None) -> Iterator[None]:
    """Context manager: load the overlay once and reuse it within the block.

    Use this in tight classification loops (e.g. a 7000-route inventory view)
    to avoid calling _load_overlay() — and therefore verdict_home() — once per
    route.  The loaded index is stored in a thread-local so xdist workers with
    different VERDICT_HOME values each keep their own copy.

    Example::

        with preload_overlay():
            for route_id in routes:
                result = lookup(provider, auth_type)
    """
    _batch.overlay = _load_overlay(env)
    try:
        yield
    finally:
        _batch.overlay = None


def _overlay_path(env: Mapping[str, str] | None = None) -> Path:
    from verdict.paths import verdict_home

    return verdict_home(env) / "connection_billing_overlay.json"


def _load_overlay(
    env: Mapping[str, str] | None = None,
) -> dict[tuple[str, str | None], BillingFact]:
    """Load the operator overlay, returning an empty index when absent or invalid."""
    global _overlay_cache

    now_ns = time.monotonic_ns()

    from verdict.paths import verdict_home

    cache_key = verdict_home(env).as_posix()

    # Fast path: same home path, recently checked — skip stat().
    if (
        _overlay_cache is not None
        and _overlay_cache[0] == cache_key
        and now_ns - _overlay_cache[3] < _OVERLAY_STAT_TTL_NS
    ):
        return _overlay_cache[2]

    path = _overlay_path(env)
    try:
        mtime = path.stat().st_mtime_ns
        if (
            _overlay_cache is not None
            and _overlay_cache[0] == cache_key
            and _overlay_cache[1] == mtime
        ):
            # File unchanged — refresh the timestamp and return the cached index.
            _overlay_cache = (cache_key, mtime, _overlay_cache[2], now_ns)
            return _overlay_cache[2]
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Cache the miss with a sentinel mtime so callers in a tight loop
        # (e.g. a 7000-route inventory view) don't repeat the stat syscall
        # for every route within the 100ms TTL window.
        _overlay_cache = (cache_key, -1, {}, now_ns)
        return {}
    if not _validate_overlay(raw):
        return {}

    index: dict[tuple[str, str | None], BillingFact] = {}
    for entry in raw.get("entries", []):
        billing = str(entry.get("billing", BILLING_UNKNOWN))
        if billing == BILLING_UNKNOWN:
            continue
        overage = str(entry.get("overage", OVERAGE_UNKNOWN))
        raw_provider = str(entry["provider"]).lower()
        # Canonicalise aliases so billing_lookup() (which also resolves aliases)
        # can find overlay entries regardless of which name the operator uses.
        # Canonical entry wins over an alias entry for the same key: only
        # overwrite if the existing entry came from an alias.
        canonical_provider = _BILLING_PROVIDER_ALIASES.get(raw_provider, raw_provider)
        auth_type: str | None = entry.get("authType")
        if isinstance(auth_type, str):
            auth_type = auth_type.lower()
        key = (canonical_provider, auth_type)
        is_canonical = raw_provider == canonical_provider
        if key not in index or is_canonical:
            index[key] = BillingFact(billing=billing, overage=overage, source=SOURCE_OVERLAY)

    _overlay_cache = (cache_key, mtime, index, now_ns)
    return index


def _validate_overlay(raw: Any) -> bool:
    """Enforce the bundled overlay schema without a runtime jsonschema dependency."""
    if not isinstance(raw, dict) or set(raw) != {"entries"} or not isinstance(raw["entries"], list):
        return False
    required = {"provider", "billing", "overage"}
    for entry in raw["entries"]:
        if not isinstance(entry, dict) or not required <= set(entry) <= required | {"authType"}:
            return False
        if not isinstance(entry["provider"], str) or not entry["provider"].strip():
            return False
        if entry["billing"] not in (BILLING_SUBSCRIPTION, BILLING_METERED, BILLING_KEYLESS):
            return False
        if entry["overage"] not in (OVERAGE_HARD_STOP, OVERAGE_METERS_TO_PAID, OVERAGE_UNKNOWN):
            return False
        if "authType" in entry and entry["authType"] not in ("oauth", "apikey"):
            return False
    return True


# ---------------------------------------------------------------------------
# Public lookup API
# ---------------------------------------------------------------------------


def lookup(
    provider: str, auth_type: str | None, *, env: Mapping[str, str] | None = None
) -> BillingFact | None:
    """Return the billing fact for ``(provider, auth_type)``, or ``None``.

    Lookup order: overlay auth-typed → overlay provider-wide → snapshot
    auth-typed → snapshot provider-wide.  ``billing="unknown"`` entries are
    excluded from both sources; they are not facts.

    ``env`` is passed to ``verdict_home`` for test isolation.
    """
    p = provider.lower()
    # Resolve known billing aliases (e.g. ``agy`` → ``antigravity``).
    p = _BILLING_PROVIDER_ALIASES.get(p, p)
    a = auth_type.lower() if isinstance(auth_type, str) else None

    # Use the pre-loaded overlay from preload_overlay() when available and no
    # custom env is requested (the batch overlay was loaded with the same env).
    batch_overlay = getattr(_batch, "overlay", None)
    overlay = batch_overlay if (env is None and batch_overlay is not None) else _load_overlay(env)
    snapshot, _ = _load_snapshot()

    # Overlay wins over snapshot; auth-typed entry wins over provider-wide.
    for index, _source in ((overlay, SOURCE_OVERLAY), (snapshot, SOURCE_SNAPSHOT)):
        if a is not None and (p, a) in index:
            return index[(p, a)]
        if (p, None) in index:
            return index[(p, None)]

    return None


# ---------------------------------------------------------------------------
# Drift detection helpers
# ---------------------------------------------------------------------------


def _omniroute_package_root() -> Path | None:
    """Find the installed OmniRoute package root via systemd, then glob fallback."""
    # Try systemd first (no live calls; just reads unit metadata).
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "--user", "show", "omniroute.service", "-p", "ExecStart"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # ExecStart={ path=/usr/bin/node argv[]=node ... /path/to/omniroute/... }
            import re

            m = re.search(r"argv\[\][^;]*?(\S+/node_modules/omniroute)\b", result.stdout)
            if m:
                return Path(m.group(1))
    except Exception:
        pass

    # Glob fallback: ~/.local/share/fnm/.../node_modules/omniroute
    fnm_base = Path.home() / ".local" / "share" / "fnm" / "node-versions"
    if fnm_base.is_dir():
        candidates = sorted(fnm_base.glob("*/installation/lib/node_modules/omniroute"))
        if candidates:
            return candidates[-1]

    return None


def check_drift(package_root: Path | None = None) -> dict[str, Any]:
    """Compare the installed OmniRoute package against the snapshot.

    Returns a dict with keys:
      ``status``: ``"ok"``, ``"drift"``, ``"unavailable"``
      ``detail``: human-readable description
      ``snapshot_version``: version recorded in snapshot
      ``snapshot_sha256``: sha256 recorded in snapshot
      ``installed_version``: version found in installed package (or None)
      ``installed_sha256``: sha256 of installed .ts file (or None)
    """
    meta = snapshot_meta()
    snap_version: str = meta.get("omniroute_version", "")
    snap_sha: str = meta.get("source_sha256", "")
    snap_file: str = meta.get("source_file", "open-sse/config/connectionBillingCatalog.ts")

    root = package_root or _omniroute_package_root()
    if root is None:
        return {
            "status": "unavailable",
            "detail": "OmniRoute package not found; cannot check drift",
            "snapshot_version": snap_version,
            "snapshot_sha256": snap_sha,
            "installed_version": None,
            "installed_sha256": None,
        }

    # Read installed version from package.json.
    pkg_json = root / "package.json"
    try:
        installed_version = json.loads(pkg_json.read_text(encoding="utf-8")).get("version", "")
    except Exception:
        installed_version = None

    # Hash the .ts file.
    ts_path = root / snap_file
    try:
        content = ts_path.read_bytes()
        installed_sha = hashlib.sha256(content).hexdigest()
    except Exception:
        installed_sha = None

    version_match = snap_version == installed_version
    sha_match = snap_sha == installed_sha

    if version_match and sha_match:
        status = "ok"
        detail = f"snapshot matches installed OmniRoute {installed_version}"
    else:
        status = "drift"
        parts = []
        if not version_match:
            parts.append(f"version: snapshot={snap_version}, installed={installed_version}")
        if not sha_match:
            parts.append(
                f"sha256: snapshot={snap_sha[:12]}…, installed="
                + (f"{installed_sha[:12]}…" if installed_sha else "unreadable")
            )
        detail = "billing catalog drift detected: " + "; ".join(parts)

    return {
        "status": status,
        "detail": detail,
        "snapshot_version": snap_version,
        "snapshot_sha256": snap_sha,
        "installed_version": installed_version,
        "installed_sha256": installed_sha,
    }


def check_overlay_conflicts(env: Mapping[str, str] | None = None) -> list[str]:
    """Return warning strings for overlay entries where an alias and its canonical both appear.

    The canonical entry wins (see ``_load_overlay``), but having both is almost always
    a configuration mistake worth flagging in the doctor output.

    Returns an empty list when the overlay is absent, invalid, or conflict-free.
    """
    path = _overlay_path(env)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not _validate_overlay(raw):
        return []

    # Scan for entries where both an alias name and the canonical name appear for the same key.
    seen_canonical: set[tuple[str, str | None]] = set()
    alias_entries: dict[tuple[str, str | None], str] = {}  # canonical key → alias name used

    for entry in raw.get("entries", []):
        billing = str(entry.get("billing", BILLING_UNKNOWN))
        if billing == BILLING_UNKNOWN:
            continue
        raw_provider = str(entry["provider"]).lower()
        canonical = _BILLING_PROVIDER_ALIASES.get(raw_provider, raw_provider)
        auth_type: str | None = entry.get("authType")
        if isinstance(auth_type, str):
            auth_type = auth_type.lower()
        key = (canonical, auth_type)
        if raw_provider == canonical:
            seen_canonical.add(key)
        else:
            alias_entries[key] = raw_provider

    warnings: list[str] = []
    for key, alias_name in alias_entries.items():
        if key in seen_canonical:
            canonical_name, auth = key
            auth_str = f", authType={auth!r}" if auth is not None else ""
            warnings.append(
                f"Billing overlay: both alias '{alias_name}' and canonical "
                f"'{canonical_name}' appear for the same key (provider='{canonical_name}'"
                f"{auth_str}); canonical wins, alias entry is redundant — remove it."
            )
    return warnings
