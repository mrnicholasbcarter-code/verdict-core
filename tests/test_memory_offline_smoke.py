"""Tests for the credential-free deterministic MemoryPlane smoke report."""

import json
from pathlib import Path

from scripts.memory_offline_smoke import main, run


def test_smoke_report_is_deterministic_and_redacted() -> None:
    first = run()
    second = run()

    assert first == second
    assert first["schema_version"] == 1
    assert first["backend"] == "sqlite"
    assert first["network"] == "disabled"
    assert first["provider"] == "not_required"
    assert first["status"] == "ready"
    assert first["redaction_proven"] is True
    assert first["search_hit_count"] == 1
    assert first["record_schema"]
    assert all(item["content_field_present"] is True for item in first["record_schema"])
    assert all(item["metadata_field_present"] is True for item in first["record_schema"])
    assert all(item["content_values_redacted"] is True for item in first["record_schema"])
    assert all(item["metadata_values_redacted"] is True for item in first["record_schema"])
    assert "verdict-memory-smoke-" not in json.dumps(first)


def test_smoke_cli_writes_canonical_json(tmp_path: Path, capsys) -> None:
    output = tmp_path / "memory_offline_smoke.json"

    main(["--output", str(output)])

    captured = capsys.readouterr().out
    assert json.loads(captured) == json.loads(output.read_text(encoding="utf-8"))
    assert output.read_text(encoding="utf-8") == json.dumps(run(), indent=2, sort_keys=True) + "\n"
