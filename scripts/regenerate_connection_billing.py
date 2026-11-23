"""Regenerate verdict/data/omniroute_connection_billing.json from the installed
OmniRoute package.

Usage:
    uv run python scripts/regenerate_connection_billing.py [--dry-run]

The script reads ``open-sse/config/connectionBillingCatalog.ts`` from the
installed OmniRoute package, parses the CATALOG array, and writes the JSON
snapshot at ``verdict/data/omniroute_connection_billing.json``.

Run this whenever OmniRoute is upgraded and the billing catalog has changed.
After running, commit both the updated snapshot and this script together.
The doctor drift check (``verdict doctor``) shows whether the snapshot is
out of date.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate the installed OmniRoute package
# ---------------------------------------------------------------------------


def _omniroute_root() -> Path:
    """Find the installed OmniRoute package root (systemd unit or fnm glob)."""
    try:
        import subprocess

        result = subprocess.run(
            ["systemctl", "--user", "show", "omniroute.service", "-p", "ExecStart"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            m = re.search(r"argv\[\][^;]*?(\S+/node_modules/omniroute)\b", result.stdout)
            if m:
                return Path(m.group(1))
    except Exception:
        pass

    fnm_base = Path.home() / ".local" / "share" / "fnm" / "node-versions"
    if fnm_base.is_dir():
        candidates = sorted(fnm_base.glob("*/installation/lib/node_modules/omniroute"))
        if candidates:
            return candidates[-1]

    raise SystemExit(
        "Cannot find OmniRoute package.  Start OmniRoute at least once so the package is installed."
    )


# ---------------------------------------------------------------------------
# Parse the TS catalog
# ---------------------------------------------------------------------------

_TS_SOURCE_FILE = "open-sse/config/connectionBillingCatalog.ts"

# Matches one CATALOG entry object: { provider: "...", authType: "...", ... }
_ENTRY_RE = re.compile(
    r"\{\s*"
    r'provider:\s*["\']([^"\']+)["\'],\s*'
    r'authType:\s*["\']([^"\']+)["\'],\s*'
    r'billing:\s*["\']([^"\']+)["\'],\s*'
    r'overage:\s*["\']([^"\']+)["\']'
    r"\s*\}",
    re.MULTILINE,
)


def _parse_catalog(ts_text: str) -> list[dict[str, str]]:
    """Extract all CATALOG entries from the TS source."""
    entries = []
    for m in _ENTRY_RE.finditer(ts_text):
        entries.append(
            {
                "provider": m.group(1),
                "authType": m.group(2),
                "billing": m.group(3),
                "overage": m.group(4),
            }
        )
    if not entries:
        raise ValueError(
            "No CATALOG entries found in TS file. "
            "The regex may need updating if the format changed."
        )
    return entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_OUTPUT_PATH = (
    Path(__file__).parent.parent / "verdict" / "data" / "omniroute_connection_billing.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print result but do not write.")
    parser.add_argument(
        "--force-shrink",
        action="store_true",
        help=(
            "Allow writing a snapshot with fewer entries than the existing one. "
            "Use only when a provider was genuinely removed from the catalog."
        ),
    )
    args = parser.parse_args()

    root = _omniroute_root()
    pkg_json = root / "package.json"
    version = json.loads(pkg_json.read_text(encoding="utf-8")).get("version", "unknown")

    ts_path = root / _TS_SOURCE_FILE
    ts_bytes = ts_path.read_bytes()
    ts_sha = hashlib.sha256(ts_bytes).hexdigest()
    ts_text = ts_bytes.decode("utf-8")

    entries = _parse_catalog(ts_text)

    # Guard: refuse to write a snapshot with fewer entries than the existing one.
    # A shrinking count almost always means the regex stopped matching (format
    # changed) rather than a genuine catalog reduction.  Fail loudly so the
    # operator can decide whether to update the regex or accept the change.
    # Pass --force-shrink to override when a provider was genuinely removed.
    if not args.force_shrink and _OUTPUT_PATH.exists():
        try:
            _prev = json.loads(_OUTPUT_PATH.read_text(encoding="utf-8"))
            _prev_count = len(_prev.get("entries", []))
            if len(entries) < _prev_count:
                print(
                    f"ERROR: parsed {len(entries)} entries but the existing snapshot has "
                    f"{_prev_count}. The source format may have changed; check the regex "
                    f"or pass --force-shrink to override.",
                    file=sys.stderr,
                )
                sys.exit(1)
        except (ValueError, KeyError):
            pass  # Existing file is corrupt — let the write proceed and repair it.

    snapshot = {
        "_meta": {
            "omniroute_version": version,
            "source_file": _TS_SOURCE_FILE,
            "source_sha256": ts_sha,
            "generated": str(date.today()),
            "note": f"Generated from {version} on {date.today()}.",
        },
        "entries": entries,
    }

    output = json.dumps(snapshot, indent=2) + "\n"

    if args.dry_run:
        print(output)
        print(f"[dry-run] would write to {_OUTPUT_PATH}", file=sys.stderr)
        return

    _OUTPUT_PATH.write_text(output, encoding="utf-8")
    print(
        f"Wrote {len(entries)} entries to {_OUTPUT_PATH} "
        f"(omniroute {version}, sha256={ts_sha[:12]}…)"
    )


if __name__ == "__main__":
    main()
