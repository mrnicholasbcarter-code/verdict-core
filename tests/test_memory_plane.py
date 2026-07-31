import pytest

from verdict.memory_plane import MemoryPlane, MemoryRecord


def test_memory_plane_put_get_search_and_scope(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        stored = plane.put(
            MemoryRecord("r1", "docs", "guide", "Local-first RAG", "git", scope="repo")
        )
        assert stored.created_at > 0
        assert plane.get("docs", "guide", scope="repo").content == "Local-first RAG"
        assert plane.search("local RAG", scope="repo")[0].record_id == "r1"
        assert plane.search("local", scope="other") == []


def test_memory_plane_replaces_fts_and_expires(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        plane.put(MemoryRecord("r1", "docs", "guide", "old text", "git"))
        plane.put(MemoryRecord("r2", "docs", "guide", "new text", "git"))
        assert plane.get("docs", "guide").record_id == "r2"
        assert plane.search("old") == []
        plane.put(MemoryRecord("r3", "docs", "gone", "ephemeral", "test", expires_at=1))
        assert plane.get("docs", "gone") is None


def test_memory_plane_tombstone_hides_content_and_preserves_history(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        stored = plane.put(
            MemoryRecord("r1", "docs", "private", "do not retrieve", "local", scope="repo")
        )

        tombstone = plane.tombstone("docs", "private", scope="repo")

        assert tombstone is not None
        assert tombstone.status == "tombstone"
        assert tombstone.supersedes == stored.record_id
        assert tombstone.content == "[tombstone]"
        assert plane.get("docs", "private", scope="repo") is None
        assert plane.search("retrieve", scope="repo") == []
        assert plane.list_namespaces(scope="repo") == []
        assert plane.export_records(scope="repo") == []
        history = plane.history("docs", "private", scope="repo")
        assert [item.status for item in history] == ["superseded", "tombstone"]
        assert plane.export_records(scope="repo", include_history=True)[-1]["status"] == (
            "tombstone"
        )
        assert plane.tombstone("docs", "private", scope="repo") is None


def test_memory_plane_tombstone_does_not_leave_an_fts_row(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        plane.put(MemoryRecord("r1", "docs", "private", "secret phrase", "local"))
        plane.tombstone("docs", "private")

        assert plane._db.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 0


def test_memory_plane_health_is_non_sensitive(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        assert plane.health() == {
            "state": "ready",
            "backend": "sqlite",
            "schema_version": 2,
            "scope": "default",
            "records": 0,
            "expired": 0,
            "semantic": "unavailable",
        }


def test_memory_plane_is_restart_safe_and_preserves_history(tmp_path):
    path = tmp_path / "memory.db"
    first = MemoryPlane(path)
    stored = first.put(
        MemoryRecord(
            "r1",
            "docs",
            "guide",
            "first version",
            "git",
            provenance={"commit": "abc"},
            confidence=0.8,
        )
    )
    first.close()

    second = MemoryPlane(path)
    replacement = second.put(MemoryRecord("r2", "docs", "guide", "second version", "git"))
    assert replacement.supersedes == stored.record_id
    assert second.get("docs", "guide").content == "second version"
    assert [item.record_id for item in second.history("docs", "guide")] == ["r1", "r2"]
    assert second.history("docs", "guide")[0].status == "superseded"
    assert second.history("docs", "guide")[0].content_hash
    second.close()


def test_memory_plane_never_accepts_caller_authority_as_verified(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        stored = plane.put(
            MemoryRecord(
                "r1",
                "decisions",
                "route",
                "advisory",
                "caller",
                authority="system",
                authority_verified=True,
            )
        )

    assert stored.authority == "system"
    assert stored.authority_verified is False


def test_memory_plane_export_import_and_namespace_listing_are_deterministic(tmp_path):
    path = tmp_path / "memory.db"
    with MemoryPlane(path) as plane:
        plane.put(MemoryRecord("b", "zeta", "b", "shared words", "local", scope="repo"))
        plane.put(MemoryRecord("a", "alpha", "a", "shared words", "local", scope="repo"))
        exported = plane.export_records(scope="repo")
        assert [item["namespace"] for item in exported] == ["alpha", "zeta"]
        assert plane.list_namespaces(scope="repo") == ["alpha", "zeta"]
        assert [item.record.record_id for item in plane.search_ranked("shared", scope="repo")] == [
            "a",
            "b",
        ]

    with MemoryPlane(tmp_path / "copy.db") as copy:
        written, duplicates = copy.import_records(exported)
        assert (written, duplicates) == (2, 0)
        assert copy.import_records(exported) == (0, 2)
        assert copy.get("alpha", "a", scope="repo").content == "shared words"


def test_memory_plane_concurrent_readers_are_scope_safe(tmp_path):
    path = tmp_path / "memory.db"
    with MemoryPlane(path) as writer:
        writer.put(MemoryRecord("r1", "docs", "guide", "private text", "local", scope="repo"))

    first = MemoryPlane(path)
    second = MemoryPlane(path)
    try:
        assert first.get("docs", "guide", scope="repo").content == "private text"
        assert second.search("private", scope="other") == []
        assert second.status(scope="repo")["records"] == 1
    finally:
        first.close()
        second.close()


def test_memory_plane_rejects_invalid_confidence_and_content_hash(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        with pytest.raises(ValueError, match="confidence"):
            plane.put(MemoryRecord("bad", "docs", "bad", "content", "local", confidence=2))
        with pytest.raises(ValueError, match="content_hash"):
            plane.put(
                MemoryRecord("bad-hash", "docs", "bad", "content", "local", content_hash="invalid")
            )
