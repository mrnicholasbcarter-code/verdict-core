#!/usr/bin/env python3
"""Generate the same-byte lifecycle-event/v1 golden vectors.

The output under ``vectors/golden/lifecycle-event-v1/`` is the language-neutral
parity fixture set: each valid case stores the exact bytes a conforming
implementation (Python today, the native Rust adapter next) must produce, plus
a manifest and a ``SHA256SUMS`` file that pin every byte.

Usage (from ``packages/verdict-memory``)::

    python scripts/gen_golden_vectors.py          # rewrite the fixtures
    python scripts/gen_golden_vectors.py --check  # exit 1 if any byte drifted

The script is deterministic: no clocks, no randomness, sorted output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PKG_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_ROOT = PKG_ROOT / "vectors" / "golden" / "lifecycle-event-v1"

sys.path.insert(0, str(PKG_ROOT / "src"))

from verdict_memory.contracts import (
    CONTRACT_API_VERSION,
    LIFECYCLE_EVENT_SCHEMA,
    LifecycleEvent,
    canonicalize,
)

# Fields of ``to_dict()`` that are NOT part of the logical digest payload.
DIGEST_EXCLUDED_FIELDS = ("content", "observed_at", "deadline_at", "idempotency_key")
IDEMPOTENCY_IDENTITY_FIELDS = ("source_harness", "project", "workspace", "tenant", "session_id")
# Files listed in SHA256SUMS are every fixture file except these.
UNPINNED_FILES = frozenset({"SHA256SUMS", "README.md", ".editorconfig", ".gitattributes"})


def _identity(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "source_harness": "prime",
        "project": "bod80-golden",
        "session_id": "sess-golden-1",
        "source_event_id": "src-evt-0001",
    }
    base.update(overrides)
    return base


# Inputs are what a harness puts on the wire. Optional fields may be omitted;
# the canonical form fills defaults. Do not edit a case in place: add a new one.
VALID_CASES: dict[str, dict[str, Any]] = {
    "minimal_source_event": {
        "description": "Smallest valid event with a harness source_event_id; every optional field omitted.",
        "input": {
            "kind": "session_start",
            "content": "session started",
            "identity": _identity(),
            "created_at": 1700000000.0,
        },
    },
    "no_source_event_id": {
        "description": "No source_event_id: idempotency_key and content_fingerprint both equal the digest.",
        "input": {
            "kind": "manual_capture",
            "content": "note captured without a harness event id",
            "identity": {
                "source_harness": "verdict",
                "project": "bod80-golden",
                "session_id": "sess-golden-2",
            },
            "created_at": 1700000100.25,
        },
    },
    "alias_before_turn": {
        "description": "Provisional alias 'before_turn' normalizes to 'before_first_model_turn' before digesting.",
        "input": {
            "kind": "before_turn",
            "content": "first turn about to run",
            "identity": _identity(source_event_id="src-evt-alias"),
            "created_at": 1700000200.0,
        },
    },
    "unknown_fields_ignored": {
        "description": "Unknown top-level and identity fields are dropped; bytes equal a clean event.",
        "input": {
            "kind": "after_tool",
            "content": "tool finished",
            "identity": {**_identity(source_event_id="src-evt-ext"), "x_future_id": "ignored"},
            "created_at": 1700000300.0,
            "x_future_field": {"nested": [1, 2, 3]},
            "x_flag": True,
        },
    },
    "observed_and_deadline_excluded": {
        "description": "observed_at and deadline_at appear in canonical.json but not in the digest payload.",
        "input": {
            "kind": "before_tool",
            "content": "about to call a tool",
            "identity": _identity(source_event_id="src-evt-clock"),
            "created_at": 1700000400.0,
            "observed_at": 1700000401.5,
            "deadline_at": 1700000460.0,
        },
    },
    "unicode_and_escapes": {
        "description": "Non-ASCII stays raw UTF-8 (ensure_ascii=False); only quote, backslash and C0 controls are escaped.",
        "input": {
            "kind": "after_edit",
            "content": (
                'quote " backslash \\ tab \t newline \n cr \r bell \u0007 unit-sep \u001f '
                "del \u007f nbsp \u00a0 line-sep \u2028 para-sep \u2029 "
                "café 日本語 路由预算 e\u0301 🚀 𝄞"
            ),
            "identity": _identity(source_event_id="src-evt-ütf8-🚀"),
            "created_at": 1700000500.0,
        },
    },
    "numeric_pinning": {
        "description": "Pinned number spellings: floats keep '.0', ints stay ints, no exponent notation.",
        "input": {
            "kind": "verification_result",
            "content": "numbers",
            "identity": _identity(source_event_id="src-evt-num"),
            "created_at": 1700000000.0,
            "payload": {
                "float_one": 1.0,
                "float_zero": 0.0,
                "float_neg_zero": -0.0,
                "float_half": 0.5,
                "float_neg": -2.25,
                "float_frac": 123456.789,
                "float_small": 0.0001,
                "float_big": 999999999999999.9,
                "int_zero": 0,
                "int_neg": -7,
                "int_big": 9007199254740993,
                "bool_true": True,
                "null_value": None,
            },
        },
    },
    "nested_key_order": {
        "description": "Keys sort by Unicode code point at every depth (UTF-8 byte order), arrays keep order.",
        "input": {
            "kind": "handoff",
            "content": "handoff to next harness",
            "identity": _identity(
                source_event_id="src-evt-order",
                parent_session_id="sess-parent-0",
                attempt_id="attempt-2",
                tenant="tenant-a",
                workspace="ws-a",
                source_agent="agent-x",
            ),
            "created_at": 1700000600.0,
            "classification": "derived",
            "derived_from": ["ref-b", "ref-a"],
            "provenance": {"tool": "pa-daemon", "Tool": "upper", "_": "underscore"},
            "payload": {
                "z": [3, 1, 2],
                "a": {"y": 1, "b": 2, "B": 3, "é": 4, "日": 5, "𝄞": 6, "10": 7, "9": 8},
            },
            "metadata": {"empty_obj": {}, "empty_arr": [], "empty_str": ""},
            "scope": "team",
            "memory_kind": "decision",
            "sensitivity": "internal",
            "trust": "local-verified",
            "revision": "2",
        },
    },
}

# Each invalid case must be rejected. ``error`` is a language-neutral code;
# the Python test maps it to the exception the reference implementation raises.
INVALID_CASES: dict[str, dict[str, Any]] = {
    "unknown_kind": {
        "description": "A kind outside the canonical set (after alias normalization) is an explicit error.",
        "error": "unknown_kind",
        "input_text": json.dumps(
            {
                "kind": "teleport",
                "content": "x",
                "identity": _identity(),
                "created_at": 1700000000.0,
            },
            ensure_ascii=False,
            indent=2,
        ),
    },
    "content_hash_mismatch": {
        "description": "A supplied content_hash that is not sha256(content) is rejected, never recomputed silently.",
        "error": "content_hash_mismatch",
        "input_text": json.dumps(
            {
                "kind": "after_edit",
                "content": "x",
                "content_hash": "0" * 64,
                "identity": _identity(),
                "created_at": 1700000000.0,
            },
            ensure_ascii=False,
            indent=2,
        ),
    },
    "unsupported_schema_version": {
        "description": "A future schema_version is a breaking change (v2) and must not be read as v1.",
        "error": "unsupported_schema_version",
        "input_text": json.dumps(
            {
                "schema_version": "lifecycle-event/v2",
                "kind": "after_edit",
                "content": "x",
                "identity": _identity(),
                "created_at": 1700000000.0,
            },
            ensure_ascii=False,
            indent=2,
        ),
    },
    "non_finite_number": {
        "description": "NaN is not JSON. Strict parsers reject it on load; the reference rejects it at canonicalization.",
        "error": "non_finite_number",
        "input_text": (
            '{\n  "kind": "after_edit",\n  "content": "x",\n'
            '  "identity": {"source_harness": "prime", "project": "bod80-golden",'
            ' "session_id": "sess-golden-1", "source_event_id": "src-evt-0001"},\n'
            '  "created_at": 1700000000.0,\n  "payload": {"bad": NaN}\n}'
        ),
    },
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return canonicalize(value).encode("utf-8")


def build_case(input_obj: dict[str, Any]) -> dict[str, bytes]:
    """Return the fixture files for one valid case (relative name -> bytes)."""
    event = LifecycleEvent.from_dict(input_obj)
    full = event.to_dict()
    digest_payload = {k: v for k, v in full.items() if k not in DIGEST_EXCLUDED_FIELDS}
    files: dict[str, bytes] = {
        "input.json": (json.dumps(input_obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        "content.txt": event.content.encode("utf-8"),
        "canonical.json": _canonical_bytes(full),
        "digest_payload.json": _canonical_bytes(digest_payload),
    }
    sid = event.identity.source_event_id
    if sid and str(sid).strip():
        fingerprint_payload = {k: v for k, v in digest_payload.items() if k != "created_at"}
        identity = event.identity.to_dict()
        idem_payload = {
            "schema_version": event.schema_version,
            "protocol_version": event.protocol_version,
            **{k: identity[k] for k in IDEMPOTENCY_IDENTITY_FIELDS},
            "source_event_id": str(sid),
        }
        files["fingerprint_payload.json"] = _canonical_bytes(fingerprint_payload)
        files["idempotency_payload.json"] = _canonical_bytes(idem_payload)

    # Self-check: the independent spec reconstruction must agree with the
    # reference implementation, or the generator refuses to write anything.
    if canonicalize(full) != event.to_canonical_json():
        raise AssertionError("canonical.json disagrees with LifecycleEvent.to_canonical_json()")
    if _sha256(files["digest_payload.json"]) != event.digest():
        raise AssertionError("digest_payload.json does not hash to LifecycleEvent.digest()")
    if _sha256(files["content.txt"]) != event.content_hash:
        raise AssertionError("content.txt does not hash to content_hash")
    if "fingerprint_payload.json" in files:
        if _sha256(files["fingerprint_payload.json"]) != event.content_fingerprint():
            raise AssertionError("fingerprint_payload.json does not hash to content_fingerprint()")
        if _sha256(files["idempotency_payload.json"]) != event.idempotency_key:
            raise AssertionError("idempotency_payload.json does not hash to idempotency_key")
    elif not (event.idempotency_key == event.digest() == event.content_fingerprint()):
        raise AssertionError(
            "without source_event_id, idempotency_key/fingerprint must equal digest"
        )
    return files


def build_tree() -> dict[str, bytes]:
    """Return every generated file (path relative to GOLDEN_ROOT -> bytes)."""
    tree: dict[str, bytes] = {}
    manifest_valid: dict[str, Any] = {}
    for name in sorted(VALID_CASES):
        case = VALID_CASES[name]
        files = build_case(case["input"])
        event = LifecycleEvent.from_dict(case["input"])
        for fname, data in files.items():
            tree[f"valid/{name}/{fname}"] = data
        manifest_valid[name] = {
            "description": case["description"],
            "kind": event.kind,
            "expect": {
                "content_hash": event.content_hash,
                "digest": event.digest(),
                "content_fingerprint": event.content_fingerprint(),
                "idempotency_key": event.idempotency_key,
            },
            "files": {fname: _sha256(data) for fname, data in sorted(files.items())},
        }
    manifest_invalid: dict[str, Any] = {}
    for name in sorted(INVALID_CASES):
        case = INVALID_CASES[name]
        data = (case["input_text"] + "\n").encode("utf-8")
        tree[f"invalid/{name}.json"] = data
        manifest_invalid[name] = {
            "description": case["description"],
            "error": case["error"],
            "sha256": _sha256(data),
        }
    manifest = {
        "schema": LIFECYCLE_EVENT_SCHEMA,
        "contract_api_version": CONTRACT_API_VERSION,
        "generator": "packages/verdict-memory/scripts/gen_golden_vectors.py",
        "canonical_json": (
            "json.dumps(value, sort_keys=True, separators=(',', ':'), "
            "ensure_ascii=False, allow_nan=False) encoded as UTF-8, no trailing newline"
        ),
        "digest_payload": "canonical.json object minus " + ", ".join(DIGEST_EXCLUDED_FIELDS),
        "fingerprint_payload": "digest payload minus created_at (only when source_event_id is set)",
        "idempotency_payload": (
            "{schema_version, protocol_version, "
            + ", ".join(IDEMPOTENCY_IDENTITY_FIELDS)
            + ", source_event_id} (only when source_event_id is set; else idempotency_key = digest)"
        ),
        "valid": manifest_valid,
        "invalid": manifest_invalid,
    }
    tree["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    sums = "".join(f"{_sha256(tree[path])}  {path}\n" for path in sorted(tree))
    tree["SHA256SUMS"] = sums.encode("utf-8")
    return tree


def _on_disk() -> dict[str, bytes]:
    if not GOLDEN_ROOT.is_dir():
        return {}
    return {
        p.relative_to(GOLDEN_ROOT).as_posix(): p.read_bytes()
        for p in sorted(GOLDEN_ROOT.rglob("*"))
        if p.is_file() and p.name not in UNPINNED_FILES - {"SHA256SUMS"}
    }


def drift(tree: dict[str, bytes]) -> list[str]:
    """Paths whose committed bytes differ from a fresh generation."""
    disk = _on_disk()
    return sorted(p for p in set(tree) | set(disk) if tree.get(p) != disk.get(p))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if fixtures drifted")
    args = parser.parse_args(argv)
    tree = build_tree()
    if args.check:
        changed = drift(tree)
        for path in changed:
            print(f"drift: {path}")
        return 1 if changed else 0
    for path, data in tree.items():
        target = GOLDEN_ROOT / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(f"wrote {len(tree)} files under {GOLDEN_ROOT.relative_to(PKG_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
