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


def test_memory_plane_health_is_non_sensitive(tmp_path):
    with MemoryPlane(tmp_path / "memory.db") as plane:
        assert plane.health() == {
            "backend": "sqlite",
            "schema_version": 1,
            "records": 0,
            "fts": True,
        }
