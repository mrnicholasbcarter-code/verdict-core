#!/usr/bin/env python3
"""Parent-only isolated live proof. Requires --allow-live-write after review.

Token stays in the client, never printed. A unique project/source namespace is
created per invocation. Only its exact validated page is deleted in finally.
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
    parser.add_argument("--project", default="bod80-memory-proof", help="isolated project prefix (unique suffix added)")
    parser.add_argument("--token-file", default=os.environ.get("AI_MEMORY_TOKEN_FILE", str(Path.home() / ".config/ai-memory/auth-token")))
    parser.add_argument("--marker", default="bod80-proof")
    parser.add_argument("--allow-live-write", action="store_true")
    args = parser.parse_args()
    if not args.allow_live_write:
        print("HOLD explicit --allow-live-write required after independent review")
        return 2

    from verdict_memory.client import AiMemoryClient, AiMemoryClientConfig
    from verdict_memory.contracts import EventIdentity, ExternalMemoryRef, LifecycleEvent, RecallQuery

    nonce = uuid.uuid4().hex
    project = f"{args.project}-{nonce}"
    marker = f"{args.marker}-{nonce}"
    client = AiMemoryClient(AiMemoryClientConfig(
        endpoint=args.endpoint, token_file=args.token_file, workspace=args.workspace,
        project=project, timeout=15.0,
    ))
    health = client.health()
    print("HEALTH", health.status.value, "version", health.provider_version,
          "workspace", args.workspace, "project", project)
    if health.status.value != "available":
        print("FAIL health", health.message)
        return 3
    event = LifecycleEvent(
        kind="manual_capture", content=f"# BOD80 P live proof\n\n{marker}\n",
        identity=EventIdentity("verdict-memory-proof", project, f"proof-{nonce}",
                               source_event_id=marker, workspace=args.workspace, source_agent="p-proof"),
        created_at=1_700_000_000.0,
    )
    expected = ExternalMemoryRef("ai-memory", f"verdict-memory/events/{event.idempotency_key}.md", event.digest())
    result_code = 4
    attempted = False
    try:
        attempted = True
        ref = client.put(event)
        if ref != expected:
            raise ValueError("put reference or digest mismatch")
        print("PUT", ref.external_id, "EVENT_DIGEST", ref.revision)
        print("SOURCE_EVENT_ID", event.identity.source_event_id, "CONTENT_FP", event.content_fingerprint())
        if client.put(event) != ref:
            raise ValueError("replay mismatch")
        result = client.search(RecallQuery(marker, project=project, workspace=args.workspace, limit=10))
        matched = [hit for hit in result.hits if hit.ref == expected]
        print("SEARCH", result.status.value, "hits", len(result.hits), "exact", len(matched))
        if result.status.value != "available" or len(matched) != 1:
            raise ValueError("exact search result missing")
        hit = matched[0]
        if (not hit.untrusted or hit.event.to_dict() != event.to_dict()
                or hit.event.digest() != event.digest()
                or hit.event.content_fingerprint() != event.content_fingerprint()
                or hit.event.identity.source_event_id != event.identity.source_event_id
                or hit.event.identity.project != project
                or hit.event.identity.workspace != args.workspace
                or hit.event.scope != event.scope):
            raise ValueError("readback lifecycle identity/content/scope mismatch")
        print("VERIFIED", hit.ref.external_id, hit.event.digest(), "untrusted", hit.untrusted)
        result_code = 0
    except Exception as exc:
        # Exception messages may include remote payloads; expose class only.
        print("FAIL", type(exc).__name__)
    finally:
        if attempted:
            try:
                deleted = client.delete(expected)
                print("CLEANUP_DELETE", expected.external_id, deleted)
                if not deleted:
                    result_code = 6
            except Exception as exc:
                print("CLEANUP_DELETE_FAIL", type(exc).__name__)
                result_code = 6
    if result_code == 0:
        print("OK backend=ai-memory exact-envelope-and-cleanup", marker)
    return result_code


if __name__ == "__main__":
    sys.exit(main())
