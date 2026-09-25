#!/usr/bin/env python3
"""OpenJev live smoke test (BOD-235).

Makes a real call to POST /v1/systemone using TYPESAFE_API_KEY.

Exit codes:
  0: call succeeded (HTTP 200)
  1: call failed (non-200 response or exception)
  2: TYPESAFE_API_KEY not set

Prints RESULT: PASS or RESULT: FAIL as the last line.
Never prints or logs the API key.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

# Fixed question set: import canonical set from the provider
import verdict
from verdict.decision_signals.openjev import _QUESTIONS as QUESTIONS


def main() -> int:
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        print("ERROR: TYPESAFE_API_KEY not set", file=sys.stderr)
        print("Export TYPESAFE_API_KEY and run again.", file=sys.stderr)
        print("RESULT: FAIL")
        return 2

    base_url = os.environ.get("TYPESAFE_BASE_URL", "https://api.codiv.ai").strip()
    model = os.environ.get("VERDICT_OPENJEV_MODEL", "openjev-0.1").strip()

    user_agent = f"verdict-core/{verdict.__version__}"

    state = "Refactor the OpenJev provider to match the real Codiv API wire format."
    payload = {"model": model, "state": state, "questions": QUESTIONS}

    url = f"{base_url.rstrip('/')}/v1/systemone"
    parsed = urllib.parse.urlparse(url)
    path = parsed.path or "/"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": user_agent,
    }

    print(f"POST {url}")
    print(f"model: {model}")
    # Never print the key; show only first 8 chars
    print(f"key: {api_key[:8]}...")

    conn_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    start = datetime.now(timezone.utc)
    try:
        conn = conn_class(parsed.netloc, timeout=30)
        try:
            conn.request("POST", path, json.dumps(payload).encode("utf-8"), headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            body_bytes = resp.read()
        finally:
            conn.close()
    except Exception as exc:
        elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"elapsed: {elapsed:.0f} ms")
        print("RESULT: FAIL")
        return 1

    elapsed = (datetime.now(timezone.utc) - start).total_seconds() * 1000
    request_id = resp_headers.get("x-typesafe-request-id", "(none)")

    print(f"status: {status}")
    print(f"x-typesafe-request-id: {request_id}")
    print(f"elapsed: {elapsed:.0f} ms")

    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        body = body_bytes.decode("utf-8", errors="replace")

    # Never include the key in output
    safe_output = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else str(body)
    if isinstance(api_key, str) and api_key and api_key in safe_output:
        safe_output = safe_output.replace(api_key, "[REDACTED]")

    if status == 200:
        print("response:")
        print(safe_output)
        print("RESULT: PASS")
        return 0
    else:
        print("error response:")
        print(safe_output)
        print("RESULT: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
