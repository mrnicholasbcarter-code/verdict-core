"""BOD-279 prep: flagship failover scenario through the REAL run loop.

Runs ``run_golden_path`` OFFLINE with:
- Pre-built WorkGraph (plan → 2 implement nodes → integrate)
- ScriptedExecutor producing real file edits in a tmp git repo
- FaultInjectingExecutor injecting ONE rate_limit (429) on route A specifically
- Real EligibilityLadder over a static route inventory (no OmniRoute)
- Real FailureIntelligence classifier
- Real OpenCodeReviewer with a scripted OCR CLI runner

Inventory design (discrimination):
  Route A (alpha/model-a) is STRICTLY BEST by the real ranking
  (same capacity class / price / capabilities; wins by lexicographic tiebreak).
  The fault is injected by exact route key, so only when the ladder picks A does
  the fault fire.  With cooldown working, the selector rejects A at the AVAILABLE
  stage and picks B.  Without cooldowns (mutation A/B), the selector would re-pick
  A (which is best) but it is excluded by the tried-set — the rejection reason
  changes from ``cooldown:provider`` to ``excluded_route``, and the ladder state
  has no cooldown entries.  This makes every mutation visible.

All assertions read from events.jsonl, the receipt, and the ladder state — never
from test-side mutable state (except the ladder reference itself, which is the
production object the run loop mutated).
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.demo_scenario import (
    FLAGSHIP_RUN_ID,
    ROUTE_A,
    ROUTE_B,
    run_flagship_scenario,
)
from verdict.orchestration.receipt import verify_run_receipt


def _events_of(
    events: list[dict[str, Any]], etype: str, node_id: str | None = None
) -> list[dict[str, Any]]:
    """Return flattened event dicts (type + node_id + data fields merged)."""
    result = []
    for e in events:
        if e.get("type") != etype:
            continue
        if node_id is not None and e.get("node_id") != node_id:
            continue
        flat = {"type": e["type"], "node_id": e.get("node_id", "")}
        flat.update(e.get("data", {}))
        result.append(flat)
    return result

def _run_scenario(tmp_path: Path, *, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Execute the shared flagship scenario used by the recording script."""
    del monkeypatch  # the shared helper isolates and restores its environment
    return run_flagship_scenario(
        tmp_path / "runs",
        workspace_root=tmp_path / "workspace",
        run_id=FLAGSHIP_RUN_ID,
    )


# ===========================================================================
# Tests
# ===========================================================================


class TestFlagshipFailoverScenario:
    """End-to-end assertions from events.jsonl, receipt, and ladder state."""

    @pytest.fixture(autouse=True)
    def scenario(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._r = _run_scenario(tmp_path, monkeypatch=monkeypatch)

    # ---- event ordering ---------------------------------------------------

    def test_global_event_sequence(self) -> None:
        """Global required-event ordering: run_started … run_finished."""
        types = [e["type"] for e in self._r.events]
        required_order = [
            "run_started",
            "understand",
            "plan_ready",
            "eligibility",
            "selection",
            "hydrate",
            "terminal",
            "verify",
            "run_finished",
        ]
        indices = []
        for req in required_order:
            idx = next((i for i, t in enumerate(types) if t == req), None)
            assert idx is not None, f"event {req!r} not found"
            indices.append(idx)
        for i in range(1, len(indices)):
            assert indices[i] > indices[i - 1], (
                f"{required_order[i]} at {indices[i]} should be after "
                f"{required_order[i - 1]} at {indices[i - 1]}"
            )

    def test_per_node_event_ordering(self) -> None:
        """For each implementation node: selection < hydrate < terminal < verify.

        No node-scoped event may appear after run_finished.
        """
        run_finished_idx = next(
            i for i, e in enumerate(self._r.events) if e["type"] == "run_finished"
        )
        per_node_types = ("selection", "hydrate", "terminal", "verify")
        for nid in ("node-1", "node-2"):
            # All node events must precede run_finished.
            last_node_idx = max(
                (
                    i
                    for i, e in enumerate(self._r.events)
                    if e.get("node_id") == nid and e["type"] in per_node_types
                ),
                default=-1,
            )
            assert last_node_idx < run_finished_idx, (
                f"{nid}: last per-node event at {last_node_idx} >= run_finished at {run_finished_idx}"
            )
            # Per-node ordering: the LAST occurrence of each type must be in order
            # (node-1 has two selections; the second is what matters for the ordering
            # relative to hydrate/terminal/verify).
            last_of: dict[str, int] = {}
            for i, e in enumerate(self._r.events):
                if e.get("node_id") == nid and e["type"] in per_node_types:
                    last_of[e["type"]] = i
            present = [t for t in per_node_types if t in last_of]
            for a, b in itertools.pairwise(present):
                assert last_of[a] < last_of[b], (
                    f"{nid}: last {a} at {last_of[a]} should precede last {b} at {last_of[b]}"
                )

    # ---- node-1 failover: first attempt fails, second succeeds ------------

    def test_first_selection_is_route_a(self) -> None:
        """First selection for node-1 is route A (the strictly-best route)."""
        selections = _events_of(self._r.events, "selection", "node-1")
        assert len(selections) >= 1
        assert selections[0]["route_id"] == ROUTE_A

    def test_node1_first_attempt_fails_rate_limit(self) -> None:
        """Node-1's first attempt fails with category rate_limited."""
        failures = _events_of(self._r.events, "failure", "node-1")
        assert len(failures) >= 1
        assert failures[0]["category"] == "rate_limited"
        assert failures[0].get("fault_injected") is True

    def test_provider_cooldown_recorded_in_events(self) -> None:
        """A provider-scope cooldown event is emitted after the rate_limit failure."""
        cooldowns = _events_of(self._r.events, "cooldown", "node-1")
        assert len(cooldowns) >= 1
        cd = cooldowns[0]
        assert cd["scope"] == "provider"
        assert cd["category"] == "rate_limited"

    def test_reassign_picks_route_b(self) -> None:
        """Reassign goes from route A to route B (the second-best route)."""
        reassigns = _events_of(self._r.events, "reassign", "node-1")
        assert len(reassigns) >= 1
        r = reassigns[0]
        assert r["from_route"] == ROUTE_A
        assert r["to_route"] == ROUTE_B

    def test_node1_validated_on_route_b(self) -> None:
        """Node-1 reaches VALIDATED state on route B."""
        validated = [
            e
            for e in _events_of(self._r.events, "node_state", "node-1")
            if e["state"] == "VALIDATED"
        ]
        assert len(validated) == 1
        assert validated[0]["route_id"] == ROUTE_B

    # ---- cooldown STATE discrimination (mutations A/B would break these) ---

    def test_ladder_state_has_cooldown_for_route_a_provider(self) -> None:
        """The ladder's persisted state has a provider-scope cooldown for alpha.

        rate_limit (429) is provider-scoped: ``record_failure`` writes both
        ``route:alpha/model-a`` and ``provider:alpha``.  Node-2 may call
        ``record_success(alpha/model-a)`` which clears the route-level entry,
        but the provider-level entry survives.

        Mutation A (skip record_failure) → no provider cooldown → FAILS.
        """
        cooldowns = self._r.ladder._state["cooldowns"]
        provider_key = "provider:alpha"
        assert provider_key in cooldowns, (
            f"cooldown entry missing for {provider_key}; keys present: {list(cooldowns.keys())}"
        )
        entry = cooldowns[provider_key]
        assert entry.get("until"), f"cooldown entry for {provider_key} has no 'until'"
        assert entry.get("category") == "rate_limited"

    def test_ladder_cooldown_is_future_dated(self) -> None:
        """The provider cooldown has an 'until' timestamp in the future.

        Mutation A (skip record_failure) → no cooldown → prior test already fails.
        This test validates the timestamp is well-formed and future-dated.
        """
        cooldowns = self._r.ladder._state["cooldowns"]
        provider_key = "provider:alpha"
        entry = cooldowns.get(provider_key, {})
        until_str = entry.get("until", "")
        assert until_str, "no 'until' in provider cooldown"
        from datetime import datetime, timezone

        until = datetime.fromisoformat(until_str)
        # Cooldown should still be in the future (rate_limit default = 60s)
        now = datetime.now(timezone.utc)
        assert until > now, f"cooldown already expired: {until} <= {now}"

    def test_dispatch_blocker_active_for_route_a(self) -> None:
        """dispatch_blocker(ROUTE_A) returns a blocking key after the run.

        Mutation A → no cooldown → returns None → FAILS.
        Mutation B → _active_cooldown returns None → returns None → FAILS.
        """
        now = datetime.now(timezone.utc)
        blocker = self._r.ladder.dispatch_blocker(ROUTE_A, now=now)
        assert blocker is not None, (
            f"dispatch_blocker returned None for {ROUTE_A}; "
            f"cooldowns: {list(self._r.ladder._state['cooldowns'].keys())}"
        )

    def test_second_eligibility_rejects_route_a_at_cooldown_stage(self) -> None:
        """The eligibility event for node-1's second selection shows route A
        rejected at the AVAILABLE stage with a cooldown reason.

        With working cooldowns: ``_assess`` hits cooldown BEFORE ``_task_gate``
        → rejected at AVAILABLE with ``cooldown:route`` or ``cooldown:provider``.

        Mutation B (_active_cooldown → None): ``_assess`` passes AVAILABLE, but
        ``_task_gate`` rejects at TASK_ELIGIBLE with ``excluded_route`` →
        the cooldown-stage rejection is absent → FAILS.
        """
        elig_events = _events_of(self._r.events, "eligibility", "node-1")
        # The second eligibility event is the one after the failure / reassignment.
        assert len(elig_events) >= 2, (
            f"expected ≥2 eligibility events for node-1, got {len(elig_events)}"
        )
        second = elig_events[1]
        rejections = second.get("rejections", {})
        # Route A must be rejected at AVAILABLE stage (not TASK_ELIGIBLE).
        available_reasons = rejections.get("AVAILABLE", {})
        cooldown_count = sum(v for k, v in available_reasons.items() if k.startswith("cooldown:"))
        assert cooldown_count > 0, (
            f"Expected route A rejected at AVAILABLE with cooldown reason, "
            f"but AVAILABLE rejections = {available_reasons}; "
            f"all rejections = {rejections}"
        )

    # ---- node-2 independent completion ------------------------------------

    def test_node2_completes_independently(self) -> None:
        """Node-2 completes without failures."""
        failures = _events_of(self._r.events, "failure", "node-2")
        assert len(failures) == 0
        validated = [
            e
            for e in _events_of(self._r.events, "node_state", "node-2")
            if e["state"] == "VALIDATED"
        ]
        assert len(validated) == 1

    # ---- verification and review ------------------------------------------

    def test_verify_passes_for_all_nodes(self) -> None:
        """VERIFY passes for both implement nodes."""
        for nid in ("node-1", "node-2"):
            verifies = _events_of(self._r.events, "verify", nid)
            ok_verifies = [v for v in verifies if v["ok"]]
            assert len(ok_verifies) >= 1, f"no passing verify for {nid}"

    def test_review_passes(self) -> None:
        """Review event has status PASS."""
        reviews = _events_of(self._r.events, "review")
        assert len(reviews) == 1
        assert reviews[0]["status"] == "PASS"

    # ---- outcome and receipt ----------------------------------------------

    def test_outcome_complete(self) -> None:
        """Run outcome is COMPLETE.

        Mutation C (stop on first worker failure) → outcome ≠ COMPLETE → FAILS.
        """
        finished = _events_of(self._r.events, "run_finished")
        assert len(finished) >= 1
        assert finished[-1]["outcome"] == "COMPLETE"

    def test_receipt_verifies_clean(self) -> None:
        """verify_run_receipt returns no problems."""
        problems = verify_run_receipt(self._r.run_dir)
        assert problems == [], f"receipt problems: {problems}"


class TestDeterminism:
    """Two runs produce the same per-node event sequences and routes."""

    def test_two_runs_same_per_node_sequences_and_routes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()

        ra = _run_scenario(dir_a, monkeypatch=monkeypatch)
        rb = _run_scenario(dir_b, monkeypatch=monkeypatch)

        from collections import Counter

        assert Counter(e["type"] for e in ra.events) == Counter(e["type"] for e in rb.events)

        for nid in ("node-1", "node-2", "integrate"):
            seq_a = [e["type"] for e in ra.events if e.get("node_id") == nid]
            seq_b = [e["type"] for e in rb.events if e.get("node_id") == nid]
            assert seq_a == seq_b, f"per-node event sequence differs for {nid}"

        for nid in ("node-1", "node-2"):
            sel_a = [e["route_id"] for e in _events_of(ra.events, "selection", nid)]
            sel_b = [e["route_id"] for e in _events_of(rb.events, "selection", nid)]
            assert sel_a == sel_b, f"selected routes differ for {nid}: {sel_a} vs {sel_b}"
