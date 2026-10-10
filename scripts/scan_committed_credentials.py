#!/usr/bin/env python3
"""Scan committed files for credentials.

Fails if any credential-like filename is committed, unless it's in the
allowlist AND (for .env templates) contains no non-empty secret values.
"""

import re
import sys
from pathlib import Path


def main() -> int:
    """Scan git-tracked files for credential patterns."""
    import subprocess

    # Get all git-tracked files
    result = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True)
    paths = [p for p in result.stdout.decode("utf-8", "surrogateescape").split("\0") if p]

    # Pattern for credential-like filenames
    pattern = re.compile(
        r"(^|/)(\.env(rc|([._-].*)?)?|[^/]*\.env|.*\.(pem|key|crt|cer|p12|pfx)"
        r"|id_(rsa|dsa|ecdsa|ed25519))$",
        re.I,
    )

    # Allowlist: templates that are safe IF they contain no real values
    allowlist = {".env.example"}

    blocked = []
    for path in paths:
        if not pattern.search(path):
            continue
        if path in allowlist:
            # Allowed template: verify it contains no non-empty secret values
            content = Path(path).read_text(encoding="utf-8")
            # Check each line: KEY=value lines must have empty value or obvious placeholder
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    value = value.strip(" \"\t'")
                    # Non-empty value that's not a placeholder is a real secret
                    if value and not any(
                        placeholder in value.lower()
                        for placeholder in [
                            "your-",
                            "example",
                            "unset",
                            "todo",
                            "replace",
                            "changeme",
                        ]
                    ):
                        print(
                            f"FAIL: {path} is in allowlist but contains non-empty value for {key.strip()}",
                            file=sys.stderr,
                        )
                        blocked.append(path)
                        break
            else:
                # Template is clean
                print(f"allowed: {path} (template with no real values)")
        else:
            print(f"blocked: {path}", file=sys.stderr)
            blocked.append(path)

    if blocked:
        print(f"blocked: {blocked}")
        print("\nRESULT: FAIL (exit 1)")
        return 1

    print("blocked: (none)")
    print("\nRESULT: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
