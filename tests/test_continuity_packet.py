"""Continuity packet: interruption, root restart, fence, and refresh."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from verdict.orchestration.continuity_packet import (
    ContinuityEvent,
    ContinuityPacketError,
    ContinuityPacketStore,
    GoverningRevisions,
    LastProof,
    ResearchFinding,
    SourceRef,
    fence_writer,
    replace_writer,
)

TASK = "t_govern_1"
LINEAGE = "attempt-lineage-1"
FENCED_AT = "2026-11-12T16:46:00Z"


def _revisions() -> GoverningRevisions:
    return GoverningRevisions(
        acceptance_criterion_ids=("AC-2",),
        spec_revision_digest="sha256:" + "ab" * 32,
        adr_id="ADR-033",
        adr_status="Accepted",
    )


def _finding(
    finding_id: str = "f1", text: str = "incremental research", revision: str = "rev-1"
) -> ResearchFinding:
    return ResearchFinding(
        finding_id=finding_id,
        source_refs=(SourceRef("src-1", revision), SourceRef("src-2", "rev-2")),
        text=text,
        revision=revision,
    )


def _open(tmp_path: Path) -> ContinuityPacketStore:
    store = ContinuityPacketStore(tmp_path)
    store.create(
        governing_task_id=TASK,
        exact_next_action="record the fence before cutover",
        last_proof=LastProof.none(),
        governing_revisions=_revisions(),
        attempt_lineage_id=LINEAGE,
        admitted_route_id="free/writer-a",
        authorization_ref="admission:free/writer-a",
        capture_refs=("outbox:capture-1",),
    )
    return store


def test_packet_round_trips_after_interruption(tmp_path: Path) -> None:
    store = _open(tmp_path)
    finding = _finding()
    proof = LastProof.command(
        command="uv run python -m pytest -q tests/test_continuity_packet.py",
        exit_code=0,
        artifact_ref="artifacts/continuity-proof.json",
    )
    store.apply_event(TASK, ContinuityEvent(event_id="evt-research", finding=finding))
    store.apply_event(
        TASK,
        ContinuityEvent(
            event_id="evt-files", changed_files=("verdict/orchestration/continuity_packet.py",)
        ),
    )
    store.apply_event(TASK, ContinuityEvent(event_id="evt-proof", last_proof=proof))
    store.apply_event(
        TASK,
        ContinuityEvent(event_id="evt-next", exact_next_action="fence the old writer, then resume"),
    )
    del store

    restored = ContinuityPacketStore(tmp_path).read(TASK)
    assert restored.governing_task_id == TASK
    assert restored.research == (finding,)
    assert restored.changed_files == ("verdict/orchestration/continuity_packet.py",)
    assert restored.last_proof == proof
    assert restored.exact_next_action == "fence the old writer, then resume"
    assert restored.capture_refs == ("outbox:capture-1",)
    assert restored.attempt_lineage_id == LINEAGE


def test_packet_round_trips_after_root_restart(tmp_path: Path) -> None:
    store = _open(tmp_path)
    finding = _finding(text="research that must survive root loss")
    proof = LastProof.command(
        command="uv run python -m pytest -q tests/test_continuity_packet.py",
        exit_code=1,
        artifact_ref="artifacts/last-proof.txt",
    )
    store.apply_event(TASK, ContinuityEvent(event_id="evt-research", finding=finding))
    store.apply_event(TASK, ContinuityEvent(event_id="evt-proof", last_proof=proof))
    store.apply_event(
        TASK, ContinuityEvent(event_id="evt-next", exact_next_action="read the durable packet")
    )
    expected_revision = store.read(TASK).packet_revision
    del store

    env = os.environ.copy()
    root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                "from verdict.orchestration.continuity_packet import ContinuityPacketStore\n"
                "packet = ContinuityPacketStore(sys.argv[1]).read(sys.argv[2])\n"
                "json.dump(packet.to_dict(), sys.stdout)\n"
            ),
            str(tmp_path),
            TASK,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
    )
    assert probe.returncode == 0, probe.stderr
    payload = json.loads(probe.stdout)
    assert payload["governing_task_id"] == TASK
    assert payload["packet_revision"] == expected_revision
    assert payload["research"] == [
        {
            "finding_id": "f1",
            "source_refs": [
                {"source_id": "src-1", "revision": "rev-1"},
                {"source_id": "src-2", "revision": "rev-2"},
            ],
            "text": "research that must survive root loss",
            "revision": "rev-1",
        }
    ]
    assert payload["last_proof"] == {
        "kind": "command",
        "command": "uv run python -m pytest -q tests/test_continuity_packet.py",
        "exit_code": 1,
        "artifact_ref": "artifacts/last-proof.txt",
    }
    assert payload["exact_next_action"] == "read the durable packet"
    assert payload["attempt_lineage_id"] == LINEAGE


def test_duplicate_event_does_not_double_apply(tmp_path: Path) -> None:
    store = _open(tmp_path)
    event = ContinuityEvent(
        event_id="evt-research",
        finding=_finding(),
        changed_files=("verdict/orchestration/continuity_packet.py",),
        capture_refs=("outbox:capture-2",),
    )
    first = store.apply_event(TASK, event)
    replay = store.apply_event(
        TASK,
        ContinuityEvent(
            event_id="evt-research",
            finding=_finding(text="a second copy that must not land"),
            changed_files=("tests/test_continuity_packet.py",),
            capture_refs=("outbox:capture-3",),
        ),
    )
    assert replay.packet_revision == first.packet_revision
    assert replay.research == first.research
    assert replay.changed_files == first.changed_files
    assert replay.capture_refs == first.capture_refs
    assert replay.event_ids_applied.count("evt-research") == 1


def test_unchanged_research_is_retained(tmp_path: Path) -> None:
    store = _open(tmp_path)
    original = _finding()
    store.apply_event(TASK, ContinuityEvent(event_id="evt-a", finding=original))
    updated = store.apply_event(
        TASK,
        ContinuityEvent(
            event_id="evt-b",
            finding=_finding(text="rewritten research that must not replace the original"),
            changed_files=("verdict/orchestration/continuity_packet.py",),
        ),
    )
    assert updated.research == (original,)
    assert updated.changed_files == ("verdict/orchestration/continuity_packet.py",)
    assert updated.packet_revision == store.read(TASK).packet_revision


def test_old_writer_is_fenced_before_replacement(tmp_path: Path) -> None:
    store = _open(tmp_path)
    with pytest.raises(ContinuityPacketError, match="not fenced"):
        store.replace_writer(TASK, replacement_writer_id="writer-b")
    rejected = store.read(TASK)
    assert rejected.replacement_writer_id is None
    assert rejected.writer_fence is None

    fenced = fence_writer(tmp_path, TASK, writer_id="writer-a", fenced_at=FENCED_AT)
    assert fenced.writer_fence is not None
    assert fenced.writer_fence.writer_id == "writer-a"
    assert fenced.writer_fence.fenced_at == FENCED_AT
    assert fenced.replacement_writer_id is None
    with pytest.raises(ContinuityPacketError, match="fenced"):
        store.apply_event(
            TASK,
            ContinuityEvent(event_id="evt-race", finding=_finding(finding_id="race")),
            writer_id="writer-a",
        )

    replaced = replace_writer(tmp_path, TASK, replacement_writer_id="writer-b")
    assert replaced.replacement_writer_id == "writer-b"
    assert replaced.writer_fence is not None
    assert replaced.writer_fence.writer_id == "writer-a"
    assert replaced.writer_fence.fenced_at == FENCED_AT
    with pytest.raises(ContinuityPacketError, match="fenced"):
        store.apply_event(
            TASK,
            ContinuityEvent(event_id="evt-old", finding=_finding(finding_id="old")),
            writer_id="writer-a",
        )
    raced = store.read(TASK)
    assert raced.research == ()
    assert raced.replacement_writer_id == "writer-b"


def test_resume_keeps_governing_task_and_uses_current_authorization(tmp_path: Path) -> None:
    store = _open(tmp_path)
    finding = _finding()
    store.apply_event(TASK, ContinuityEvent(event_id="evt-research", finding=finding))
    store.fence_writer(TASK, writer_id="writer-a", fenced_at=FENCED_AT)
    before = store.read(TASK)

    with pytest.raises(ContinuityPacketError, match="governing task"):
        store.resume(
            TASK,
            requested_governing_task_id="other-task",
            admitted_route_id="free/writer-b",
            authorization_ref="admission:eligible:free/writer-b",
            source_revisions={"src-1": "rev-1", "src-2": "rev-2"},
        )
    unchanged = store.read(TASK)
    assert unchanged.governing_task_id == TASK
    assert unchanged.admitted_route_id == "free/writer-a"
    assert unchanged.research == before.research

    resumed = store.resume(
        TASK,
        requested_governing_task_id=TASK,
        admitted_route_id="free/writer-b",
        authorization_ref="admission:eligible:free/writer-b",
        source_revisions={"src-1": "rev-1", "src-2": "rev-2"},
    )
    assert resumed.governing_task_id == TASK
    assert resumed.admitted_route_id == "free/writer-b"
    assert resumed.authorization_ref == "admission:eligible:free/writer-b"
    assert resumed.research == (finding,)
    assert resumed.attempt_lineage_id == LINEAGE


def test_refresh_skips_unchanged_sources(tmp_path: Path) -> None:
    store = _open(tmp_path)
    finding = ResearchFinding(
        finding_id="f1",
        source_refs=(
            SourceRef("src-1", "rev-1"),
            SourceRef("src-2", "rev-2"),
            SourceRef("src-missing", "rev-3"),
        ),
        text="incremental research",
        revision="rev-1",
    )
    store.apply_event(TASK, ContinuityEvent(event_id="evt-research", finding=finding))
    store.fence_writer(TASK, writer_id="writer-a", fenced_at=FENCED_AT)

    resumed = store.resume(
        TASK,
        requested_governing_task_id=TASK,
        admitted_route_id="free/writer-b",
        authorization_ref="admission:eligible:free/writer-b",
        source_revisions={"src-1": "rev-1", "src-2": "rev-changed", "src-new": "rev-9"},
    )
    assert "src-1" in resumed.retained_source_ids
    assert "src-1" not in resumed.refreshed_source_ids
    assert "src-2" in resumed.refreshed_source_ids
    assert "src-2" not in resumed.retained_source_ids
    assert "src-missing" in resumed.refreshed_source_ids
    assert "src-missing" not in resumed.retained_source_ids
    assert "src-new" in resumed.refreshed_source_ids
    assert resumed.research == (finding,)


def test_retained_text_redacts_a_secret_canary(tmp_path: Path) -> None:
    store = _open(tmp_path)
    canary = "api_key=canary-secret-value"
    store.apply_event(
        TASK,
        ContinuityEvent(
            event_id="evt-canary",
            finding=_finding(text=f"note {canary} end"),
            capture_refs=(canary,),
        ),
    )
    restored = ContinuityPacketStore(tmp_path).read(TASK)
    rendered = json.dumps(restored.to_dict())
    assert canary not in rendered
    assert "[REDACTED]" in restored.research[0].text
