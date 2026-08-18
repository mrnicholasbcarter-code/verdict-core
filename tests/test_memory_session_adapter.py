from __future__ import annotations

import json
from pathlib import Path

from verdict.memory_plane import MemoryPlane
from verdict.memory_session_adapter import (
    SessionAdapter,
    SessionDiscoveryPolicy,
    SessionImportPolicy,
    import_discovered_sessions,
    import_session,
    poll_discovered_sessions,
    verify_session_manifest,
)


def write_jsonl(path: Path, records: list[object]) -> None:
    path.write_bytes(b"\n".join(json.dumps(record).encode() for record in records) + b"\n")


def test_jsonl_normalizes_roles_tools_and_namespace_without_provider_access(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    write_jsonl(
        source,
        [
            {"schema_version": 1, "role": "human", "content": "hello"},
            {"schema_version": 1, "role": "model", "text": "hi", "id": "m1"},
            {
                "schema_version": 1,
                "event_type": "tool_call",
                "tool_name": "search",
                "arguments": {"query": "safe"},
            },
        ],
    )

    result = import_session(source, project="demo_app", session_id="s-1")

    assert result.report.status == "ok"
    assert [record["role"] for record in result.records] == ["user", "assistant", "tool"]
    assert result.records[2]["event_type"] == "tool_call"
    assert result.records[2]["tool_name"] == "search"
    assert result.manifest["namespace"] == "project/demo_app/session/s-1"
    assert result.manifest["manifest_hash"]


def test_credentials_and_raw_prompts_are_redacted_by_default(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    write_jsonl(
        source,
        [
            {
                "schema_version": 1,
                "role": "user",
                "prompt": "raw private prompt",
                "metadata": {"api_key": "sk-super-secret-value"},
            },
            {
                "schema_version": 1,
                "event_type": "tool_result",
                "content": "Authorization: Bearer secret-token-value",
            },
        ],
    )

    result = import_session(source, project="p", session_id="s")
    encoded = json.dumps(result.records, sort_keys=True)

    assert result.report.redacted >= 2
    assert "raw private prompt" not in encoded
    assert "sk-super-secret-value" not in encoded
    assert "secret-token-value" not in encoded


def test_malformed_partial_records_are_isolated(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    source.write_bytes(
        b'{"schema_version":1,"role":"assistant","content":"ok"}\n'
        b"not-json\n"
        b'{"schema_version":2,"role":"assistant","content":"new"}\n'
        b"[]\n"
    )

    result = import_session(source, project="p", session_id="s")

    assert result.report.status == "partial"
    assert result.report.records_seen == 4
    assert result.report.records_accepted == 1
    assert result.report.skipped == 3
    assert len(result.report.errors) == 3


def test_file_line_and_record_limits_are_bounded(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    write_jsonl(
        source,
        [
            {"schema_version": 1, "role": "assistant", "content": "a"},
            {"schema_version": 1, "role": "assistant", "content": "b" * 100},
        ],
    )

    line_limited = import_session(
        source, project="p", session_id="s", policy=SessionImportPolicy(max_line_bytes=70)
    )
    assert line_limited.report.status == "partial"
    assert line_limited.report.records_accepted == 1

    record_limited = import_session(
        source, project="p", session_id="s", policy=SessionImportPolicy(max_records=1)
    )
    assert record_limited.report.status == "partial"
    assert record_limited.report.records_accepted == 1
    assert "record limit exceeded" in record_limited.report.errors

    file_limited = import_session(
        source, project="p", session_id="s", policy=SessionImportPolicy(max_file_bytes=5)
    )
    assert file_limited.report.status == "error"
    assert "max_file_bytes" in file_limited.report.errors[0]


def test_manifests_are_deterministic_and_unsupported_formats_are_explicit(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    write_jsonl(source, [{"schema_version": 1, "role": "assistant", "content": "stable"}])

    first = SessionAdapter().import_session(source, project="p", session_id="s")
    second = SessionAdapter().import_session(source, project="p", session_id="s")
    assert first.manifest == second.manifest
    assert verify_session_manifest(first.manifest)
    tampered = dict(first.manifest)
    tampered["records"] = list(first.manifest["records"])
    tampered["records"][0] = dict(tampered["records"][0])
    tampered["records"][0]["content"] = "tampered"
    assert not verify_session_manifest(tampered)

    unsupported = import_session(
        tmp_path / "provider.db", project="p", session_id="s", format="sqlite"
    )
    assert unsupported.report.status == "unavailable"
    assert unsupported.records == ()
    assert "unsupported" in unsupported.report.errors[0]


def test_provider_export_descriptors_accept_wrapped_jsonl(tmp_path: Path) -> None:
    source = tmp_path / "events.jsonl"
    for provider in ("claude", "codex", "pi"):
        write_jsonl(
            source, [{"schema_version": 1, "message": {"role": "assistant", "content": "ok"}}]
        )
        result = SessionAdapter().import_file(
            source, project="p", session_id=provider, format=f"{provider}-jsonl"
        )
        assert result.report.status == "ok"
        assert result.manifest["format"] == f"{provider}-jsonl"
        assert result.records[0]["provenance"]["format"] == f"{provider}-jsonl"


def test_invalid_utf8_is_reported_without_aborting_other_lines(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    source.write_bytes(b'{"schema_version":1,"role":"assistant","content":"ok"}\n\xff\xfe\n')

    result = import_session(source, project="p", session_id="s")
    assert result.report.status == "partial"
    assert result.report.records_accepted == 1
    assert "UTF-8" in result.report.errors[0]


def test_session_records_convert_to_memory_plane_records(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    write_jsonl(source, [{"schema_version": 1, "role": "assistant", "content": "durable"}])
    result = import_session(source, project="p", session_id="s")
    record = result.memory_records[0]

    with MemoryPlane(tmp_path / "memory.db") as plane:
        stored = plane.put(record)
        assert stored.content == "durable"
        assert plane.get(record.namespace, record.key, scope=record.scope) == stored


def test_discovers_known_provider_jsonl_locations_latest_first(tmp_path: Path) -> None:
    files = {
        "claude": tmp_path / ".claude" / "projects" / "demo" / "claude.jsonl",
        "codex": tmp_path / ".codex" / "sessions" / "codex.jsonl",
        "pi": tmp_path / ".pi-subagents" / "artifacts" / "abc123_worker_0_transcript.jsonl",
        "ruflo": tmp_path / ".claude-flow" / "sessions" / "ruflo.jsonl",
    }
    for index, path in enumerate(files.values(), start=1):
        path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(path, [{"schema_version": 1, "role": "assistant", "content": path.stem}])
        timestamp = 1_700_000_000 + index
        path.touch()
        import os

        os.utime(path, (timestamp, timestamp))

    result = SessionAdapter().discover_sessions(SessionDiscoveryPolicy(roots=(tmp_path,)))

    assert result.report.status == "ok"
    assert result.report.candidates_found == 4
    assert [candidate.provider for candidate in result.candidates] == [
        "ruflo",
        "pi",
        "codex",
        "claude",
    ]
    assert {candidate.format for candidate in result.candidates} == {
        "claude-jsonl",
        "codex-jsonl",
        "pi-jsonl",
        "ruflo-jsonl",
    }
    assert all(candidate.file_sha256 for candidate in result.candidates)


def test_discovery_skips_symlinks_and_oversized_files(tmp_path: Path) -> None:
    valid = tmp_path / ".codex" / "sessions" / "valid.jsonl"
    valid.parent.mkdir(parents=True)
    write_jsonl(valid, [{"schema_version": 1, "role": "assistant", "content": "ok"}])
    oversized = tmp_path / ".codex" / "sessions" / "large.jsonl"
    oversized.write_text("x" * 128, encoding="utf-8")
    link = tmp_path / ".codex" / "sessions" / "linked.jsonl"
    link.symlink_to(valid)

    result = SessionAdapter().discover_sessions(
        SessionDiscoveryPolicy(roots=(tmp_path,), providers=("codex",), max_file_bytes=96)
    )

    assert [candidate.path for candidate in result.candidates] == [valid]
    assert result.report.skipped_files == 2


def test_auto_import_dry_run_reports_without_records_and_import_redacts(tmp_path: Path) -> None:
    source = tmp_path / ".claude" / "projects" / "demo" / "session.jsonl"
    source.parent.mkdir(parents=True)
    write_jsonl(
        source,
        [
            {"schema_version": 1, "role": "user", "prompt": "keep me private"},
            {
                "schema_version": 1,
                "event_type": "tool_result",
                "content": "Authorization: Bearer private-token-value",
            },
        ],
    )
    discovery_policy = SessionDiscoveryPolicy(roots=(tmp_path,), providers=("claude",))

    dry_run = import_discovered_sessions(
        project="demo", discovery_policy=discovery_policy, dry_run=True
    )
    assert dry_run.report.dry_run is True
    assert dry_run.report.candidates_found == 1
    assert dry_run.report.files_imported == 0
    assert dry_run.records == ()

    imported = import_discovered_sessions(
        project="demo", discovery_policy=discovery_policy, dry_run=False
    )
    encoded = json.dumps(imported.records, sort_keys=True)
    assert imported.report.files_imported == 1
    assert imported.report.records_emitted == 2
    assert imported.report.redacted_fields >= 2
    assert "keep me private" not in encoded
    assert "private-token-value" not in encoded
    assert imported.manifests[0]["format"] == "claude-jsonl"


def test_auto_import_sha256_deduplicates_identical_session_files(tmp_path: Path) -> None:
    first = tmp_path / ".codex" / "sessions" / "first.jsonl"
    second = tmp_path / ".codex" / "sessions" / "second.jsonl"
    first.parent.mkdir(parents=True)
    write_jsonl(first, [{"schema_version": 1, "role": "assistant", "content": "same"}])
    second.write_bytes(first.read_bytes())

    result = import_discovered_sessions(
        project="demo",
        discovery_policy=SessionDiscoveryPolicy(roots=(tmp_path,), providers=("codex",)),
        dry_run=False,
    )

    assert result.report.candidates_found == 2
    assert result.report.files_imported == 1
    assert result.report.duplicate_files == 1
    assert result.report.records_emitted == 1


def test_poll_discovered_sessions_tracks_seen_hashes_between_iterations(tmp_path: Path) -> None:
    source = tmp_path / ".pi-subagents" / "artifacts" / "abc_worker_0_transcript.jsonl"
    source.parent.mkdir(parents=True)
    write_jsonl(source, [{"schema_version": 1, "role": "assistant", "content": "once"}])

    results = list(
        poll_discovered_sessions(
            project="demo",
            discovery_policy=SessionDiscoveryPolicy(roots=(tmp_path,), providers=("pi",)),
            interval_seconds=0,
            iterations=2,
            dry_run=False,
        )
    )

    assert [item.report.files_imported for item in results] == [1, 0]
    assert [item.report.duplicate_files for item in results] == [0, 1]
