"""Memory-fed worker briefs: cited recall, and empty-healthy vs unavailable.

The autodev worker brief (``compile_unit_context``) is the production path that
compiles context for a worker before any model call. It must carry bounded,
cited prior decisions from the mandatory ai-memory plane, and it must say
explicitly whether a recall that found nothing was a healthy empty result or a
memory outage. Recalled prose is untrusted evidence: it is cited, never treated
as an instruction, and it never authorizes a route or certifies completion.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from verdict.autodev_run import compile_unit_context
from verdict.context_intelligence import recall_memory_for_task
from verdict.memory_gate import MemoryGate, MemoryWriteRequest
from verdict.memory_plane import MemoryPlane
from verdict.work_unit import WorkUnit


def _unit(objective: str = "Update compute_widget without changing its contract") -> WorkUnit:
    return WorkUnit(
        unit_id="widget",
        objective=objective,
        owned_files=("pkg/widget.py",),
        verification_command=("python", "-m", "pytest", "tests/test_widget.py"),
    )


def _seed(path: Path, rows: tuple[tuple[str, str, str], ...]) -> None:
    with MemoryPlane(path) as plane:
        gate = MemoryGate(plane)
        for namespace, key, value in rows:
            result = gate.write(
                MemoryWriteRequest(
                    namespace, key, value, "verdict-core", provenance={"source": "fixture"}
                )
            )
            assert result.allowed, result.reason


def test_recall_reports_cited_facts_with_source_revisions(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    _seed(
        path,
        (
            ("decisions", "widget-contract", "compute_widget keeps integer input; ADR-7."),
            ("patterns", "unrelated", "Rotate the dashboard colours weekly."),
        ),
    )
    recall = recall_memory_for_task("Update compute_widget", plane_path=path)
    assert recall.status == "ok"
    assert recall.available is True
    cited = [item for item in recall.citations if "compute_widget" in item.content]
    assert len(cited) == 1
    assert cited[0].record_id
    assert cited[0].source_revision.startswith("sha256:")
    assert cited[0].namespace == "decisions"
    assert all("dashboard" not in item.content for item in recall.citations)


def test_healthy_empty_is_distinct_from_unavailable(tmp_path: Path) -> None:
    present = tmp_path / "present.db"
    _seed(present, (("decisions", "other", "unrelated note about colours"),))
    empty = recall_memory_for_task("compute_widget contract", plane_path=present)
    assert empty.status == "empty"
    assert empty.available is True
    assert empty.citations == ()

    missing = recall_memory_for_task("compute_widget", plane_path=tmp_path / "absent.db")
    assert missing.status == "unavailable"
    assert missing.available is False

    broken = tmp_path / "broken.db"
    broken.write_text("not a sqlite database")
    bad = recall_memory_for_task("compute_widget", plane_path=broken)
    assert bad.status == "unavailable"
    assert bad.available is False


def test_incompatible_schema_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    _seed(path, (("decisions", "k", "compute_widget note"),))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE memory_meta SET value='999' WHERE key='schema_version'")
    recall = recall_memory_for_task("compute_widget", plane_path=path)
    assert recall.status == "unavailable"
    assert recall.available is False


def test_recall_never_writes_the_plane(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    _seed(path, (("decisions", "k", "compute_widget keeps integer input"),))
    before = path.read_bytes()
    recall_memory_for_task("compute_widget", plane_path=path)
    assert path.read_bytes() == before


def test_worker_brief_carries_cited_memory_before_any_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory.db"
    _seed(path, (("decisions", "widget-contract", "compute_widget keeps integer input; ADR-7."),))
    monkeypatch.setenv("VERDICT_MEMORY_PLANE_PATH", str(path))
    pack = compile_unit_context(_unit(), tmp_path)
    rendered = "\n".join(unit.content for unit in pack.units)
    # The cited fact and the record it came from both reach the brief.
    assert "compute_widget keeps integer input; ADR-7." in rendered
    assert "memory:decisions:widget-contract" in rendered
    assert "revision=sha256:" in rendered
    # Recall is evidence in the brief, compiled before any model is called.
    assert any("prior_verified_outcomes" in unit.key for unit in pack.units)


def test_worker_brief_records_empty_versus_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = tmp_path / "present.db"
    _seed(present, (("decisions", "other", "unrelated note about colours"),))
    monkeypatch.setenv("VERDICT_MEMORY_PLANE_PATH", str(present))
    empty_pack = compile_unit_context(_unit(), tmp_path)
    empty_text = "\n".join(unit.content for unit in empty_pack.units)
    assert "memory: empty" in empty_text
    assert "unrelated note" not in empty_text

    monkeypatch.setenv("VERDICT_MEMORY_PLANE_PATH", str(tmp_path / "absent.db"))
    down_pack = compile_unit_context(_unit(), tmp_path)
    down_text = "\n".join(unit.content for unit in down_pack.units)
    assert "memory: unavailable" in down_text
    assert "unrelated note" not in down_text
