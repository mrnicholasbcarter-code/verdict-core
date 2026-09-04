"""Scope-isolation boundary tests for ``verdict.memory_plane`` (T018).

MemoryPlane is the raw durable store; MemoryGate owns content redaction. These
checks therefore focus on the storage boundary: content written in one scope
must never be returned by another scope through any public retrieval method.
All content is fabricated and safe for test use.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tests.security.fixtures import SECRET_KEYED_VALUES
from verdict.memory_plane import MemoryPlane, MemoryRecord


@pytest.fixture
def plane() -> MemoryPlane:
    with tempfile.TemporaryDirectory() as tmp, MemoryPlane(path=Path(tmp) / "boundary.db") as store:
        yield store


def _put_secret(plane: MemoryPlane, *, scope: str = "authorized") -> tuple[str, str]:
    secret = SECRET_KEYED_VALUES["api_key"]
    key = "scope-isolation"
    content = json.dumps({"note": f"synthetic credential {secret}"})
    plane.put(
        MemoryRecord(
            record_id="plane-boundary-1",
            namespace="security-boundary",
            key=key,
            content=content,
            source="security-boundary-test",
            scope=scope,
            confidence=1.0,
            sensitivity="internal",
        )
    )
    return key, secret


def test_secret_content_is_retrievable_only_in_its_authorized_scope(plane: MemoryPlane) -> None:
    key, secret = _put_secret(plane)

    authorized = plane.get("security-boundary", key, scope="authorized")
    assert authorized is not None
    assert secret in authorized.content

    for unauthorized_scope in ("default", "other-tenant", ""):
        assert plane.get("security-boundary", key, scope=unauthorized_scope) is None
        assert plane.history("security-boundary", key, scope=unauthorized_scope) == []
        assert plane.search(secret, namespace="security-boundary", scope=unauthorized_scope) == []
        assert plane.records(namespace="security-boundary", scope=unauthorized_scope) == []
        assert plane.export_records(scope=unauthorized_scope) == []
        assert "security-boundary" not in plane.list_namespaces(scope=unauthorized_scope)
        assert plane.status(scope=unauthorized_scope)["records"] == 0


def test_scope_isolation_applies_to_fts_and_record_listing(plane: MemoryPlane) -> None:
    key, secret = _put_secret(plane, scope="tenant-a")

    assert plane.search("synthetic credential", scope="tenant-a")[0].key == key
    assert plane.search("synthetic credential", scope="tenant-b") == []
    assert all(record.scope == "tenant-a" for record in plane.records(scope="tenant-a"))
    assert plane.records(scope="tenant-b") == []
    exported = plane.export_records(scope="tenant-a")
    assert len(exported) == 1
    assert secret in exported[0]["content"]
    assert plane.export_records(scope="tenant-b") == []
