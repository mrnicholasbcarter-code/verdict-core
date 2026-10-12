"""Focused regressions for worker context credential precision."""

from __future__ import annotations

import json
from pathlib import Path

from verdict.context_pack import ContextUnit
from verdict.orchestration.contracts import WorkNode
from verdict.orchestration.worker_context import _cited, build_worker_context


def _node(*, owned_files: tuple[str, ...] = ("pkg/service.py",)) -> WorkNode:
    return WorkNode(
        "precision",
        "Update configure_client while preserving token annotations",
        owned_files=owned_files,
        required_context=(),
        verification_command=("true",),
        acceptance=("configure_client remains typed",),
    )


def _write_package(tmp_path: Path, source: str) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/service.py").write_text(source)


def test_type_annotations_and_env_expressions_are_allowed(tmp_path: Path) -> None:
    _write_package(
        tmp_path,
        "from __future__ import annotations\n"
        "import os\n\n"
        "def configure_client(token: str | None, api_key: str | None = None) -> str:\n"
        "    selected = token or os.getenv('SERVICE_TOKEN') or api_key\n"
        "    return selected or 'missing'\n",
    )

    context = build_worker_context(_node(), repo=tmp_path)

    assert context.pack is not None
    assert "def configure_client(token: str | None, api_key: str | None = None)" in context.text
    assert "os.getenv('SERVICE_TOKEN')" in context.text
    assert any(unit.source_uri == "pkg/service.py" for unit in context.pack.units)
    assert "unsafe_or_sensitive_source_excluded_before_aggregation" not in context.omissions


def test_real_secret_literal_beyond_excerpt_still_excludes_full_source(tmp_path: Path) -> None:
    _write_package(
        tmp_path,
        "def configure_client(token: str | None) -> str:\n"
        "    return token or 'missing'\n"
        + "# harmless padding\n" * 400
        + "api_key = 'fixture-real-secret'\n",
    )

    context = build_worker_context(_node(), repo=tmp_path)

    assert context.pack is not None
    assert "fixture-real-secret" not in context.text
    assert not any(unit.source_uri == "pkg/service.py" for unit in context.pack.units)
    assert "unreadable_oversized_or_sensitive_source" in context.omissions


def test_native_aggregate_filter_allows_token_annotations_but_blocks_secret_literals(
    tmp_path: Path,
) -> None:
    _write_package(
        tmp_path,
        "def configure_client(token: str | None, api_key: str | None = None) -> str:\n"
        "    return token or api_key or 'missing'\n",
    )
    (tmp_path / "pkg/secret_client.py").write_text(
        "def configure_client(token: str | None) -> str:\n"
        "    api_key = 'fixture-real-secret'\n"
        "    return token or api_key\n"
    )
    node = WorkNode(
        "precision",
        "Update configure_client while preserving token annotations",
        owned_files=("pkg/service.py", "pkg/secret_client.py"),
        required_context=(),
        verification_command=("true",),
        acceptance=("configure_client remains typed",),
    )

    context = build_worker_context(node, repo=tmp_path)

    assert context.pack is not None
    assert "pkg/service.py" in context.text
    assert "def configure_client(token: str | None, api_key: str | None = None)" in context.text
    assert "fixture-real-secret" not in context.text
    assert not any(unit.source_uri == "pkg/secret_client.py" for unit in context.pack.units)
    assert "unsafe_or_sensitive_source_excluded_before_aggregation" in context.omissions


def test_cited_redacts_retrieval_excerpt_and_clears_exact_span(tmp_path: Path) -> None:
    _write_package(
        tmp_path, "def configure_client(token: str | None) -> str:\n    return token or 'missing'\n"
    )
    unit = ContextUnit(
        unit_id="fixture",
        slot_type="evidence",
        key="retrieved:service",
        content=(
            "def configure_client(token: str | None) -> str:\n"
            "    auth = 'Bearer token-1234567890'\n"
            "    return token or auth\n"
        ),
        source_uri="pkg/service.py",
        source_digest="sha256:" + "0" * 64,
        authority="fixture",
        span={"start_line": 1, "end_line": 3},
    )

    cited = _cited(unit, tmp_path, {})

    assert cited is not None
    assert "Bearer token-1234567890" not in cited.content
    assert "Bearer [redacted]" in cited.content
    assert cited.span is None
    citation = json.loads(cited.content.splitlines()[0].removeprefix("CITE: "))
    assert citation["span"] is None
    assert "worker-redacted-secret" in cited.transform_lineage


def test_compiler_guard_still_excludes_unredacted_secret_units(tmp_path: Path) -> None:
    unit = ContextUnit(
        unit_id="fixture-secret",
        slot_type="evidence",
        key="retrieved:secret",
        content="api_key = 'fixture-real-secret'\n",
        source_uri="code://fixture",
        source_digest="sha256:" + "1" * 64,
        authority="fixture",
        transform_lineage=("source-filtered", "worker-citation"),
    )

    cited = _cited(unit, tmp_path, {id(unit): unit})

    assert cited is not None
    assert "fixture-real-secret" not in cited.content
    assert "[redacted]" in cited.content


def test_production_orchestration_files_not_dropped_for_token_annotations() -> None:
    repo = Path(__file__).resolve().parents[1]
    node = WorkNode(
        "production-annotations",
        "Inspect orchestration run cli executors token api_key annotations",
        owned_files=(
            "verdict/orchestration/run.py",
            "verdict/orchestration/cli.py",
            "verdict/orchestration/executors.py",
        ),
        required_context=(),
        verification_command=("true",),
        acceptance=("token annotations do not look like credentials",),
    )

    context = build_worker_context(node, repo=repo)

    assert context.pack is not None
    assert "api_key: str | None" in context.text
    assert any(unit.source_uri == "verdict/orchestration/run.py" for unit in context.pack.units)
    assert any(unit.source_uri == "verdict/orchestration/cli.py" for unit in context.pack.units)
    assert any(
        unit.source_uri == "verdict/orchestration/executors.py" for unit in context.pack.units
    )


def test_credentials_with_alphabetic_bearer_and_multiple_json_fields_are_excluded(
    tmp_path: Path,
) -> None:
    from verdict.orchestration.worker_context import (
        _contains_worker_context_secret,
        _redact_worker_context_secrets,
    )

    credentials = (
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        '{"token": "abcdefghijklmnop1234", "other": "x"}',
        '{"other": "x", "api_key": "abcdefghijklmnop1234"}',
    )
    for index, source in enumerate(credentials):
        assert _contains_worker_context_secret(source)
        redacted = _redact_worker_context_secrets(source)
        assert "abcdefghijklmnop" not in redacted
        assert "[redacted]" in redacted
        directory = tmp_path / str(index)
        directory.mkdir()
        _write_package(
            directory, "def configure_client():\n    return None\n" + "# padding\n" * 400 + source
        )
        context = build_worker_context(_node(), repo=directory)
        assert "abcdefghijklmnop" not in context.text
        assert context.pack is not None
        assert not any(unit.source_uri == "pkg/service.py" for unit in context.pack.units)
