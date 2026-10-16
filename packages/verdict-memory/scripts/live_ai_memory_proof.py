#!/usr/bin/env python3
"""Isolated authenticated ai-memory round-trip proof for verdict-memory.

Loads token from --token-file / AI_MEMORY_TOKEN_FILE only (never prints it).
Uses isolated project bod80-memory-test. Verifies returned ref path + marker
content + config scope — not an arbitrary old search hit.

Exit 0 only when health + put + exact search hit + delete succeed.
Parent reruns after security review; no global auth/config mutation.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
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
    parser.add_argument("--marker", default="")
    args = parser.parse_args()
    marker = args.marker.strip() or f"bod80-p-live-proof-{uuid.uuid4().hex[:12]}"

    from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
    from verdict_memory.contracts import EventIdentity, LifecycleEvent, RecallQuery

    token_path = Path(args.token_file)
    if not token_path.is_file():
        print("FAIL token_file_missing")
        return 2
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
        "project",
        args.project,
        "workspace",
        args.workspace,
    )
    if health.status.value != "available":
        print("FAIL health", getattr(client, "last_error", None))
        return 3

    event = LifecycleEvent(
        kind="manual_capture",
        content=f"# BOD80 P live proof\n\nmarker={marker}\n",
        identity=EventIdentity(
            source_harness="verdict-memory-proof",
            project=args.project,
            session_id=f"proof-{marker[-12:]}",
            source_event_id=marker,
            workspace=args.workspace,
            source_agent="p-owner",
        ),
        created_at=1_700_000_000.0,
    )
    expected_path_suffix = f"verdict-memory/memory/{marker[:24]}.md"
    content_fp = event.content_fingerprint()
    ref = None
    try:
        ref = client.put(event)
        print("PUT", ref.provider_id, ref.external_id)
        print("EVENT_DIGEST", event.digest())
        print("CONTENT_FP", content_fp)
        print("IDEMPOTENCY_KEY", event.idempotency_key)
        if expected_path_suffix not in ref.external_id and ref.external_id != expected_path_suffix:
            # Accept exact or trailing match from server
            if not str(ref.external_id).endswith(marker[:24] + ".md"):
                print("FAIL put_ref_mismatch expected_suffix", expected_path_suffix, "got", ref.external_id)
                return 7

        # Deterministic query by unique marker (no sleep; FTS should see committed write)
        result = client.search(
            RecallQuery(
                query=marker,
                project=args.project,
                workspace=args.workspace,
                limit=5,
            )
        )
        print("SEARCH", result.status.value, "hits", len(result.hits), "capability", result.capability)
        if result.status.value != "available":
            print("FAIL search_status", result.error_code, result.message, client.last_error)
            return 4
        matched = [
            h for h in result.hits
            if marker in (h.event.content or "")
            and (h.ref.external_id == ref.external_id or marker[:24] in h.ref.external_id)
        ]
        if not matched:
            print("FAIL search_no_exact_hit", "ref", ref.external_id, "paths", [h.ref.external_id for h in result.hits])
            return 4
        hit = matched[0]
        print("HIT_PATH", hit.ref.external_id, "untrusted", hit.untrusted)
        if not hit.untrusted:
            print("FAIL hit_not_untrusted")
            return 5
        if args.project not in (hit.event.identity.project,):
            print("FAIL hit_project_scope", hit.event.identity.project)
            return 8

        deleted = client.delete(ref)
        print("DELETE", deleted)
        if not deleted:
            print("FAIL delete")
            return 6
        ref = None
        print("OK backend=ai-memory version", health.provider_version, "marker", marker)
        return 0
    finally:
        if ref is not None:
            try:
                client.delete(ref)
                print("CLEANUP_DELETE", ref.external_id)
            except Exception as exc:  # noqa: BLE001
                print("CLEANUP_DELETE_FAIL", type(exc).__name__)


if __name__ == "__main__":
    sys.exit(main())
