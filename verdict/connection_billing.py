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
from collections.abc import Mapping
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

_overlay_cache: tuple[float, dict[tuple[str, str | None], BillingFact]] | None = None


def _overlay_path(env: Mapping[str, str] | None = None) -> Path:
    from verdict.paths import verdict_home

    return verdict_home(env) / "connection_billing_overlay.json"


def _load_overlay(
    env: Mapping[str, str] | None = None,
) -> dict[tuple[str, str | None], BillingFact]:
    """Load the operator overlay, returning an empty index when absent or invalid."""
    global _overlay_cache

    path = _overlay_path(env)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return {}

    if _overlay_cache is not None and _overlay_cache[0] == mtime:
        return _overlay_cache[1]

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    # Validate against schema if jsonschema is available.
    import contextlib

    with contextlib.suppress(Exception):
        _validate_overlay(raw)

    index: dict[tuple[str, str | None], BillingFact] = {}
    for entry in raw.get("entries", []):
        billing = str(entry.get("billing", BILLING_UNKNOWN))
        if billing == BILLING_UNKNOWN:
            continue
        overage = str(entry.get("overage", OVERAGE_UNKNOWN))
        provider = str(entry["provider"]).lower()
        auth_type: str | None = entry.get("authType")
        if isinstance(auth_type, str):
            auth_type = auth_type.lower()
        key = (provider, auth_type)
        index[key] = BillingFact(billing=billing, overage=overage, source=SOURCE_OVERLAY)

    _overlay_cache = (mtime, index)
    return index


def _validate_overlay(raw: Any) -> None:
    """Validate the overlay JSON against the bundled schema.  Raises on failure."""
    schema_path = Path(__file__).parent / "schemas" / "connection-billing-overlay.v1.json"
    if not schema_path.exists():
        return
    try:
        import jsonschema
    except ImportError:
        return
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(raw, schema)


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

    overlay = _load_overlay(env)
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
