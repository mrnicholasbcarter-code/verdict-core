"""Same-byte golden vectors for lifecycle-event/v1 (Python side of Rust parity).

Every file under ``vectors/golden/lifecycle-event-v1/`` is pinned twice: by the
``SHA256SUMS`` file next to it and by the digests hard-coded below. A native
implementation loads the same bytes and must reproduce them exactly; see the
README in that directory.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from verdict_memory import LifecycleEvent, canonicalize, loads_lifecycle_event

PKG_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = PKG_ROOT / "vectors" / "golden" / "lifecycle-event-v1"
UNPINNED = {"SHA256SUMS", "README.md", ".editorconfig", ".gitattributes"}
DIGEST_EXCLUDED_FIELDS = ("content", "observed_at", "deadline_at", "idempotency_key")

# Changing any fixture byte must also change this file on purpose.
EXPECTED_SHA256SUMS_DIGEST = "37b43ffb68724bb7196d29b5b05a8ba283e9b65fe48e8f9cf0a19c9c0181f1f8"
EXPECTED: dict[str, dict[str, str]] = {
    "alias_before_turn": {
        "content_fingerprint": "66c2d03238ee8ef6e6217bd2f6a27897dae44305306b94a2ac87003981dd0be5",
        "content_hash": "629e63a340a1f9650d86866334b78eb7a86708558048a52aee7099b1c4772bad",
        "digest": "2dc8073e249b9522dcfe4348aa042bf25aafb6e53d87517db9cc8c484758cac0",
        "idempotency_key": "9d2088a82c22a6c5d828d8c56d60edb8e7c4c2ba1e2c2cd9c8af80f78d8dda65",
    },
    "minimal_source_event": {
        "content_fingerprint": "fc7da07fa9a7768770ccd9e55b073b0f008a3c0e1e38f7ea65c079df2a45f349",
        "content_hash": "17b5eb9c1e75705cdf28125022958ec4f59be1dc98bd2d32862d630b1dce8341",
        "digest": "046cf42709d86cee1529476ec12c602a04973ea913e65a8669162fe090f508fb",
        "idempotency_key": "a4e92cd3c001aea22563b198671327380007bc05aaa4cca9380b7a3b5eca26b0",
    },
    "nested_key_order": {
        "content_fingerprint": "1fa3e6145feb3ad2b0ba2c23fde0e2f81499603faa7099024dd14512bd7048ec",
        "content_hash": "aa73258d4984f08b66c31979fbffa6b932244ece0977c41c9c5ebec23fa096d7",
        "digest": "945008c06e97e2049402febf959fcb4f1585b5823fc3df65b824261e093e0712",
        "idempotency_key": "66058f5dfe4049a11162e766725ce3aab406fc1481e791713ac7e048d06fd6bb",
    },
    "no_source_event_id": {
        "content_fingerprint": "df2b2d9939e102e5507168d31d6f60904278d1bc989ab36db1987f9d4c4c2572",
        "content_hash": "b5ddaab2195f2770be5fae2e30c9afb0ffce27812bdd231d772d49145047f33a",
        "digest": "df2b2d9939e102e5507168d31d6f60904278d1bc989ab36db1987f9d4c4c2572",
        "idempotency_key": "df2b2d9939e102e5507168d31d6f60904278d1bc989ab36db1987f9d4c4c2572",
    },
    "numeric_pinning": {
        "content_fingerprint": "51e18a96a2dabe3f3203c5f68cc3681c6fd50bf936fe8a5c3e999851c2e2f2e8",
        "content_hash": "f3c7807d475073ba009bf4801b2d934e9f0126cb96dd19a27dbffcae23a7f5a3",
        "digest": "a87c7a0c70d832437f957cd2f77ba0e9bc23a3b69f0c3f5189977e24bcc391c5",
        "idempotency_key": "59a77a968990580a4848e4656909312a8458b14bc7f39029fce04cb08e6c4e42",
    },
    "observed_and_deadline_excluded": {
        "content_fingerprint": "525f10c589df8158c164c0c9152a408c212e86660e6e993a8d730a502ff66b31",
        "content_hash": "cb37f36cb7d96f595d13e95df7d0a297dba13cb732d2cd79ae09a7b23800e82f",
        "digest": "56767a2198532431ea6547a98f41970c942552dc8c540953d636ebf20f9e24cf",
        "idempotency_key": "5d9e5838e42a8ed030330b45f3feec7c4d7fcff60a098372b8ceda67232a7994",
    },
    "unicode_and_escapes": {
        "content_fingerprint": "0b2f01e56feaf90475cdcd553a937b1b373a1e1f44a9bfa7f1bf12af8e19f069",
        "content_hash": "6adba7f848129c76eab00ff891ea7c14835da15da34e4f04182b6d75264d5313",
        "digest": "e3762ff6e4def61c427873da481f89fd66726ca85d416dde6b93a1be8474b36c",
        "idempotency_key": "269265b7b8d0112665804c002c3ead1388805439258984a66b75ff4c17035701",
    },
    "unknown_fields_ignored": {
        "content_fingerprint": "f137dbfbd0ff5daf7674aa28aead07acc92a5cf9673daeed1ea982aaf06339a8",
        "content_hash": "82bf1cd82cedce3a57d43e210f5aebc2b9cf6c7433fe72e2a6fd825394ca6719",
        "digest": "05f880e7144a54bafd190ad0e9cbbdf82c5d1a5149254fe3fc31e597fe38122d",
        "idempotency_key": "0be68b07b33fc5d5edfb9e2a34c988f8d137bd529613c9d0a28c974fb3fdbff3",
    },
}

INVALID_ERRORS: dict[str, tuple[type[Exception], str]] = {
    "unknown_kind": (ValueError, "unsupported event kind"),
    "content_hash_mismatch": (ValueError, "content_hash does not match content"),
    "unsupported_schema_version": (ValueError, "unsupported schema_version"),
    "non_finite_number": (ValueError, "non-finite floats are not allowed"),
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))
    return data


def _case_bytes(case: str, name: str) -> bytes:
    return (GOLDEN / "valid" / case / name).read_bytes()


def _load_generator() -> ModuleType:
    path = PKG_ROOT / "scripts" / "gen_golden_vectors.py"
    spec = importlib.util.spec_from_file_location("gen_golden_vectors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sha256sums_file_is_pinned() -> None:
    assert _sha256((GOLDEN / "SHA256SUMS").read_bytes()) == EXPECTED_SHA256SUMS_DIGEST


def test_sha256sums_cover_every_fixture_byte() -> None:
    listed: dict[str, str] = {}
    for line in (GOLDEN / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, path = line.split("  ", 1)
        listed[path] = digest
    on_disk = {
        p.relative_to(GOLDEN).as_posix()
        for p in GOLDEN.rglob("*")
        if p.is_file() and p.name not in UNPINNED
    }
    assert set(listed) == on_disk
    for path, digest in listed.items():
        assert _sha256((GOLDEN / path).read_bytes()) == digest, path


def test_manifest_matches_hardcoded_expectations() -> None:
    manifest = _manifest()
    assert set(manifest["valid"]) == set(EXPECTED)
    for case, expect in EXPECTED.items():
        assert manifest["valid"][case]["expect"] == expect, case
        for name, digest in manifest["valid"][case]["files"].items():
            assert _sha256(_case_bytes(case, name)) == digest, (case, name)
    assert set(manifest["invalid"]) == set(INVALID_ERRORS)


def test_generator_reproduces_committed_bytes() -> None:
    generator = _load_generator()
    assert generator.drift(generator.build_tree()) == []


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_python_reproduces_same_bytes(case: str) -> None:
    expect = EXPECTED[case]
    event = loads_lifecycle_event(_case_bytes(case, "input.json"))
    canonical = _case_bytes(case, "canonical.json")
    digest_payload = _case_bytes(case, "digest_payload.json")

    # Canonical bytes: exact UTF-8, no trailing newline, stable on reload.
    assert event.to_canonical_json().encode("utf-8") == canonical
    assert not canonical.endswith(b"\n")
    reloaded = LifecycleEvent.from_dict(json.loads(canonical))
    assert reloaded.to_canonical_json().encode("utf-8") == canonical

    # Digest payload is the canonical object minus the excluded fields.
    stripped = {k: v for k, v in json.loads(canonical).items() if k not in DIGEST_EXCLUDED_FIELDS}
    assert canonicalize(stripped).encode("utf-8") == digest_payload
    assert _sha256(digest_payload) == event.digest() == expect["digest"]

    assert _case_bytes(case, "content.txt") == event.content.encode("utf-8")
    assert _sha256(_case_bytes(case, "content.txt")) == event.content_hash
    assert event.content_hash == expect["content_hash"]
    assert event.content_fingerprint() == expect["content_fingerprint"]
    assert event.idempotency_key == expect["idempotency_key"]

    case_dir = GOLDEN / "valid" / case
    if event.identity.source_event_id:
        fingerprint = _case_bytes(case, "fingerprint_payload.json")
        idempotency = _case_bytes(case, "idempotency_payload.json")
        assert _sha256(fingerprint) == expect["content_fingerprint"]
        assert _sha256(idempotency) == expect["idempotency_key"]
        assert "created_at" not in json.loads(fingerprint)
    else:
        assert not (case_dir / "fingerprint_payload.json").exists()
        assert not (case_dir / "idempotency_payload.json").exists()
        assert expect["idempotency_key"] == expect["digest"] == expect["content_fingerprint"]


def test_semantics_pinned_by_specific_cases() -> None:
    alias = json.loads(_case_bytes("alias_before_turn", "canonical.json"))
    assert alias["kind"] == "before_first_model_turn"

    unknown = _case_bytes("unknown_fields_ignored", "canonical.json")
    assert b"x_future" not in unknown and b"x_flag" not in unknown

    clocks = "observed_and_deadline_excluded"
    canonical = json.loads(_case_bytes(clocks, "canonical.json"))
    assert canonical["observed_at"] == 1700000401.5
    assert canonical["deadline_at"] == 1700000460.0
    digest_payload = json.loads(_case_bytes(clocks, "digest_payload.json"))
    assert not set(DIGEST_EXCLUDED_FIELDS) & set(digest_payload)

    numeric = _case_bytes("numeric_pinning", "canonical.json")
    for needle in (
        b'"created_at":1700000000.0',
        b'"float_one":1.0',
        b'"float_zero":0.0',
        b'"float_neg_zero":-0.0',
        b'"float_small":0.0001',
        b'"float_big":999999999999999.9',
        b'"int_big":9007199254740993',
        b'"int_zero":0,',
    ):
        assert needle in numeric, needle
    payload = numeric[numeric.index(b'"payload":') : numeric.index(b',"protocol_version"')]
    assert re.search(rb"\d[eE][+-]?\d", payload) is None, "exponent notation"

    escapes = _case_bytes("unicode_and_escapes", "canonical.json")
    for needle in (b'\\"', b"\\\\", b"\\t", b"\\n", b"\\r", b"\\u0007", b"\\u001f"):
        assert needle in escapes, needle
    for raw in ("\u007f", "\u00a0", "\u2028", "\u2029", "日本語", "🚀", "𝄞", "e\u0301"):
        assert raw.encode("utf-8") in escapes, raw
    assert b"\\u00" not in escapes.replace(b"\\u0007", b"").replace(b"\\u001f", b"")

    nested = _case_bytes("nested_key_order", "canonical.json")
    order = [b'"10":7', b'"9":8', b'"B":3', b'"b":2', b'"y":1', '"é":4'.encode(), '"日":5'.encode()]
    positions = [nested.index(n) for n in order]
    assert positions == sorted(positions)
    assert b'"derived_from":["ref-b","ref-a"]' in nested


@pytest.mark.parametrize("case", sorted(INVALID_ERRORS))
def test_invalid_vectors_are_rejected(case: str) -> None:
    exc_type, message = INVALID_ERRORS[case]
    raw = (GOLDEN / "invalid" / f"{case}.json").read_bytes()
    assert _manifest()["invalid"][case]["sha256"] == _sha256(raw)
    with pytest.raises(exc_type, match=message):
        event = loads_lifecycle_event(raw)
        canonicalize(event.to_dict())


def test_non_finite_vector_is_not_strict_json() -> None:
    raw = (GOLDEN / "invalid" / "non_finite_number.json").read_text(encoding="utf-8")

    def reject(token: str) -> Any:
        raise ValueError(f"non-standard JSON constant: {token}")

    with pytest.raises(ValueError, match="non-standard JSON constant: NaN"):
        json.loads(raw, parse_constant=reject)


def test_legacy_vectors_match_bundled_copies() -> None:
    bundled = PKG_ROOT / "src" / "verdict_memory" / "vectors"
    for legacy in sorted((PKG_ROOT / "vectors").glob("*.json")):
        assert (bundled / legacy.name).read_bytes() == legacy.read_bytes(), legacy.name
