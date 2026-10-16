#!/usr/bin/env python3
"""Isolated authenticated ai-memory round-trip proof for verdict-memory.

Loads token from AI_MEMORY_TOKEN_FILE or ~/.config/ai-memory/auth-token.
Never prints the token. Uses project bod80-memory-test (created on write).
Exit 0 only when health + put + search(hit) + delete succeed.

Parent reruns this after security review; P does not mutate global auth/config.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=os.environ.get("AI_MEMORY_ENDPOINT", "http://127.0.0.1:49374"))
    parser.add_argument("--workspace", default="default")
    parser.add_argument("--project", default="bod80-memory-test")
    parser.add_argument(
        "--token-file",
        default=os.environ.get(
            "AI_MEMORY_TOKEN_FILE",
            str(Path.home() / ".config" / "ai-memory" / "auth-token"),
        ),
    )
    parser.add_argument("--marker", default="bod80-p-live-proof")
    args = parser.parse_args()

    from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
    from verdict_memory.contracts import EventIdentity, LifecycleEvent, RecallQuery

    token_path = Path(args.token_file)
    if not token_path.is_file():
        print("FAIL token_file_missing", token_path)
        return 2
    # Load in-process only; never print.
    token = token_path.read_text("utf-8").strip()
    if not token:
        print("FAIL token_empty")
        return 2

    client = AiMemoryClient(
        AiMemoryClientConfig(
            endpoint=args.endpoint,
            token=token,
            token_file=None,
            workspace=args.workspace,
            project=args.project,
            timeout=15.0,
        )
    )
    health = client.health()
    print(
        "HEALTH",
        health.status.value,
        health.state.value,
        "version",
        health.provider_version,
        "auth",
        health.auth_state,
        "capability",
        health.capability,
    )
    if health.status.value != "available":
        print("FAIL health", client.last_error)
        return 3

    event = LifecycleEvent(
        kind="manual_capture",
        content=f"# BOD80 P live proof\n\nmarker={args.marker}",
        identity=EventIdentity(
            source_harness="verdict-memory-proof",
            project=args.project,
            session_id="proof-sess-local",
            source_event_id=args.marker,
            workspace=args.workspace,
            source_agent="p-owner",
        ),
        created_at=1_700_000_000.0,
    )
    ref = client.put(event)
    print("PUT", ref.provider_id, ref.external_id)

    time.sleep(1.0)
    result = client.search(
        RecallQuery(
            query=args.marker,
            project=args.project,
            workspace=args.workspace,
            limit=5,
        )
    )
    print("SEARCH", result.status.value, "hits", len(result.hits), "capability", result.capability)
    if result.status.value != "available" or not result.hits:
        print("FAIL search", result.error_code, result.message, client.last_error)
        return 4
    hit_paths = [h.ref.external_id for h in result.hits]
    print("HIT_PATHS", hit_paths)
    if not all(h.untrusted for h in result.hits):
        print("FAIL hit_not_untrusted")
        return 5

    deleted = client.delete(ref)
    print("DELETE", deleted)
    if not deleted:
        print("FAIL delete")
        return 6

    print("OK backend=ai-memory project=", args.project, "workspace=", args.workspace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
