from __future__ import annotations

import pytest

from verdict.compatibility_manifest import (
    CROSS_REPO_CONTRACTS,
    build_compatibility_manifest,
    check_compatibility,
)


def test_manifest_generation_is_deterministic() -> None:
    first = build_compatibility_manifest()
    second = build_compatibility_manifest()

    assert first.manifest_hash == second.manifest_hash
    assert first.contracts == second.contracts
    assert set(first.contracts) == {c.__name__ for c in CROSS_REPO_CONTRACTS}


def test_check_passes_when_declared_matches_current() -> None:
    manifest = build_compatibility_manifest()

    result = check_compatibility(declared=dict(manifest.contracts))

    assert result.allowed is True
    assert result.mismatched_contracts == ()


def test_check_fails_closed_on_hash_mismatch() -> None:
    manifest = build_compatibility_manifest()
    declared = dict(manifest.contracts)
    a_contract = next(iter(declared))
    declared[a_contract] = "sha256:" + "0" * 64

    result = check_compatibility(declared=declared)

    assert result.allowed is False
    assert a_contract in result.mismatched_contracts
    assert result.reason == "contract_hash_mismatch"


def test_check_fails_closed_on_missing_declaration() -> None:
    manifest = build_compatibility_manifest()
    declared = dict(manifest.contracts)
    del declared[next(iter(declared))]

    result = check_compatibility(declared=declared)

    assert result.allowed is False
    assert result.reason == "missing_declaration"


def test_check_fails_closed_on_empty_declaration() -> None:
    result = check_compatibility(declared={})

    assert result.allowed is False
    assert result.reason == "missing_declaration"


def test_check_ignores_unknown_extra_declarations() -> None:
    manifest = build_compatibility_manifest()
    declared = dict(manifest.contracts)
    declared["SomeFutureContract"] = "sha256:" + "1" * 64

    result = check_compatibility(declared=declared)

    assert result.allowed is True


def test_manifest_to_dict_and_from_dict_round_trip() -> None:
    manifest = build_compatibility_manifest()

    restored = type(manifest).from_dict(manifest.to_dict())

    assert restored == manifest


def test_manifest_rejects_unsupported_schema_version() -> None:
    from verdict.compatibility_manifest import CompatibilityManifest

    manifest = build_compatibility_manifest()
    payload = manifest.to_dict()
    payload["schema_version"] = "999"

    with pytest.raises(ValueError, match="schema_version"):
        CompatibilityManifest.from_dict(payload)
