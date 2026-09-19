#!/usr/bin/env python3
"""Deterministic local secret-path scan (no external upload).

Scans the git index for credential-shaped filenames. Secrets are never sent to
an external evaluator — results stay on the local machine / CI runner.
"""

from __future__ import annotations

import re
import subprocess
import sys

ALLOWED = {".env.memory.example", ".env.example"}
PATTERN = re.compile(
    r"(^|/)"
    r"("
    r"\.envrc([._-].*)?"
    r"|\.env([._-].*)?"
    r"|[^/]*\.env"
    r"|.*\.(pem|key|crt|cer|der|p12|pfx)(\.[^/]*)?"
    r"|id_(rsa|dsa|ecdsa|ed25519)(\.[^/]*)?"
    r")$",
    re.IGNORECASE,
)


def main() -> int:
    if __import__("os").environ.get("PROOF_UPLOAD_SECRETS"):
        print("refusing to run with PROOF_UPLOAD_SECRETS set", file=sys.stderr)
        return 2
    completed = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=False)
    if completed.returncode != 0:
        print(completed.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return completed.returncode or 1
    paths = (path.decode("utf-8", "surrogateescape") for path in completed.stdout.split(b"\0"))
    blocked = [path for path in paths if path and path not in ALLOWED and PATTERN.search(path)]
    if blocked:
        print("Committed credential-shaped files:", file=sys.stderr)
        print("\n".join(blocked), file=sys.stderr)
        return 1
    print("secrets_scan: ok (local-only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
