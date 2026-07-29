from __future__ import annotations

import json
from pathlib import Path

from verdict.memory_plane import MemoryPlane
from verdict.memory_session_adapter import (
    SessionAdapter,
    SessionImportPolicy,
    import_session,
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
