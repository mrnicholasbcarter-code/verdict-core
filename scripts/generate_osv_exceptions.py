#!/usr/bin/env python3
"""Regenerate osv-scanner.toml's IgnoredVulns from config/security_exceptions.json (BOD-342).

config/security_exceptions.json is the single source of truth for pillow's scoped,
expiring CVE exception. This script renders the equivalent osv-scanner.toml
`[[IgnoredVulns]]` entries so the two tools never drift. Run after editing
config/security_exceptions.json; `--check` fails if osv-scanner.toml is stale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from security_exceptions import DEFAULT_EXCEPTIONS_PATH, load_exceptions

ROOT = Path(__file__).resolve().parents[1]
OSV_CONFIG_PATH = ROOT / "osv-scanner.toml"

MARKER_START = "# BEGIN generated IgnoredVulns -- run: python scripts/generate_osv_exceptions.py\n"
MARKER_END = "# END generated IgnoredVulns\n"


def render_block(exceptions_path: Path = DEFAULT_EXCEPTIONS_PATH) -> str:
    lines = [MARKER_START]
    for exc in load_exceptions(exceptions_path):
        lines.append("[[IgnoredVulns]]\n")
        lines.append(f'id = "{exc.id}"\n')
        lines.append(f"ignoreUntil = {exc.expires}\n")
        lines.append(
            f'reason = "{exc.reason} (package={exc.package} version={exc.version}; '
            f'removal: {exc.removal_trigger})"\n'
        )
        lines.append("\n")
    lines.append(MARKER_END)
    return "".join(lines)


def render_full(
    config_path: Path = OSV_CONFIG_PATH, exceptions_path: Path = DEFAULT_EXCEPTIONS_PATH
) -> str:
    text = config_path.read_text(encoding="utf-8")
    block = render_block(exceptions_path)
    if MARKER_START in text and MARKER_END in text:
        before = text.split(MARKER_START)[0]
        after = text.split(MARKER_END)[1]
        return before + block + after
    return text.rstrip("\n") + "\n\n" + block


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if osv-scanner.toml is stale")
    args = parser.parse_args()

    rendered = render_full()
    current = OSV_CONFIG_PATH.read_text(encoding="utf-8")
    if args.check:
        if rendered != current:
            print(
                "osv-scanner.toml is stale; run: python scripts/generate_osv_exceptions.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OSV_CONFIG_PATH.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
