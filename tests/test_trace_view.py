"""Tests for verdict.orchestration.trace_view (BOD-279)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_flagship_failover_scenario import (
    _run_scenario,
)
from verdict.orchestration.trace_view import (
    SCHEMA_VERSION,
    trace_view,
    trace_view_from_events,
)

# ---------------------------------------------------------------------------
# Flagship failover scenario
# ---------------------------------------------------------------------------


class TestTraceViewFlagship:
    """Trace view from the flagship failover scenario."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
        self.run_dir = result.run_dir
        self.events = result.events
        self.tv = trace_view(result.run_dir)

    def test_schema_version(self) -> None:
        assert self.tv.schema_version == SCHEMA_VERSION

    def test_run_id_and_goal(self) -> None:
        assert self.tv.run_id
        assert self.tv.goal == "flagship failover test"

    def test_steps_sorted_by_seq(self) -> None:
        seqs = [s.seq for s in self.tv.steps]
        assert seqs == sorted(seqs)

    def test_steps_cover_key_kinds(self) -> None:
        kinds = {s.kind for s in self.tv.steps}
        # Flagship scenario exercises these
        for k in ("request", "routing", "selection", "dispatch", "terminal",
                   "failure", "cooldown", "reassign", "verify", "run_finished"):
            assert k in kinds, f"missing kind: {k}"

    def test_per_node_order(self) -> None:
        """Within each node, steps are in seq order."""
        for nid in self.tv.node_ids():
            steps = self.tv.steps_for_node(nid)
            seqs = [s.seq for s in steps]
            assert seqs == sorted(seqs), f"node {nid} steps not sorted"

    def test_failure_before_cooldown_before_reassign(self) -> None:
        """For node-1: failure < cooldown < reassign in seq order."""
        n1 = self.tv.steps_for_node("node-1")
        failures = [s for s in n1 if s.kind == "failure"]
        cooldowns = [s for s in n1 if s.kind == "cooldown"]
        reassigns = [s for s in n1 if s.kind == "reassign"]
        assert failures, "no failure steps for node-1"
        assert cooldowns, "no cooldown steps for node-1"
        assert reassigns, "no reassign steps for node-1"
        assert failures[0].seq < cooldowns[0].seq < reassigns[0].seq

    def test_context_view_included(self) -> None:
        assert self.tv.context_view is not None
        assert "schema_version" in self.tv.context_view

    def test_routing_view_included(self) -> None:
        assert self.tv.routing_view is not None
        assert "schema_version" in self.tv.routing_view

    def test_to_dict_round_trip(self) -> None:
        d = self.tv.to_dict()
        assert d["schema_version"] == SCHEMA_VERSION
        assert isinstance(d["steps"], list)
        assert len(d["steps"]) == len(self.tv.steps)
        # Each step dict has required keys
        for step_d in d["steps"]:
            assert "seq" in step_d
            assert "kind" in step_d
            assert "evidence" in step_d

    def test_to_json_valid(self) -> None:
        j = self.tv.to_json(indent=2)
        parsed = json.loads(j)
        assert parsed["schema_version"] == SCHEMA_VERSION

    def test_steps_by_kind(self) -> None:
        dispatches = self.tv.steps_by_kind("dispatch")
        assert len(dispatches) >= 2  # at least 2 dispatches (node-1 and node-2)

    def test_node_ids(self) -> None:
        nids = self.tv.node_ids()
        assert "node-1" in nids
        assert "node-2" in nids


# ---------------------------------------------------------------------------
# Trace from events (no disk)
# ---------------------------------------------------------------------------


class TestTraceViewFromEvents:
    """Build trace from in-memory events."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
        self.run_dir = result.run_dir
        from verdict.orchestration.receipt import EventLog
        log = EventLog(result.run_dir / "events.jsonl")
        self.run_events = log.read()
        self.tv = trace_view_from_events(self.run_events)

    def test_same_step_count_as_disk(self) -> None:
        tv_disk = trace_view(self.run_dir)
        assert len(self.tv.steps) == len(tv_disk.steps)

    def test_steps_sorted(self) -> None:
        seqs = [s.seq for s in self.tv.steps]
        assert seqs == sorted(seqs)


# ---------------------------------------------------------------------------
# Committed proof runs
# ---------------------------------------------------------------------------


class TestTraceViewCommittedProof:
    """Committed proof runs produce deterministic trace output."""

    @pytest.mark.parametrize(
        "proof_dir",
        [
            "docs/proof/demo-run",
            "docs/proof/live-controller-run",
        ],
    )
    def test_proof_run_trace(self, proof_dir: str) -> None:
        run_dir = Path(proof_dir)
        if not (run_dir / "events.jsonl").exists():
            pytest.skip(f"{proof_dir} not present")
        tv = trace_view(run_dir)
        assert tv.schema_version == SCHEMA_VERSION
        assert len(tv.steps) > 0
        # Steps sorted
        seqs = [s.seq for s in tv.steps]
        assert seqs == sorted(seqs)
        # Determinism
        tv2 = trace_view(run_dir)
        assert len(tv.steps) == len(tv2.steps)
        for s1, s2 in zip(tv.steps, tv2.steps, strict=True):
            assert s1.seq == s2.seq
            assert s1.kind == s2.kind

    @pytest.mark.parametrize(
        "proof_dir",
        [
            "docs/proof/harness-independence-2026-09-28/prime-run",
            "docs/proof/harness-independence-2026-09-28/direct-gateway-run",
        ],
    )
    def test_harness_independence_trace(self, proof_dir: str) -> None:
        run_dir = Path(proof_dir)
        if not (run_dir / "events.jsonl").exists():
            pytest.skip(f"{proof_dir} not present")
        tv = trace_view(run_dir)
        assert tv.schema_version == SCHEMA_VERSION
        assert len(tv.steps) > 0
        seqs = [s.seq for s in tv.steps]
        assert seqs == sorted(seqs)


# ---------------------------------------------------------------------------
# Step evidence content
# ---------------------------------------------------------------------------


class TestStepEvidence:
    """Each step carries meaningful evidence from the original event."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _run_scenario(tmp_path, monkeypatch=monkeypatch)
        self.tv = trace_view(result.run_dir)

    def test_request_has_goal(self) -> None:
        req = self.tv.steps_by_kind("request")
        assert len(req) == 1
        assert req[0].evidence.get("goal") == "flagship failover test"

    def test_dispatch_has_route_id(self) -> None:
        for d in self.tv.steps_by_kind("dispatch"):
            assert "route_id" in d.evidence

    def test_failure_has_category(self) -> None:
        for f in self.tv.steps_by_kind("failure"):
            assert "category" in f.evidence

    def test_selection_has_route_id(self) -> None:
        for s in self.tv.steps_by_kind("selection"):
            assert "route_id" in s.evidence

    def test_verify_has_ok(self) -> None:
        for v in self.tv.steps_by_kind("verify"):
            assert "ok" in v.evidence
