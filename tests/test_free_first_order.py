"""Tests for free-first worker order with agentic-probe qualification.

Covers AC1-AC7 of free-first Story 3:
1. Implementation-worker order: FREE > SUBSCRIPTION > METERED; UNKNOWN opt-in.
2. Agentic gate: FREE route + agentic PASS -> implementation; single-call only -> chat.
3. Role split: frontier_worthy -> subscription-first; implementation -> free-first.
4. Determinism: same inputs -> same choice; max 8 probes; read-only cache.
5. Hard gates keep priority.
6. Receipts/events: capacity_class, probe_class, checked_at, freshness.
7. Stale-vs-fresh, UNKNOWN opt-in off/on, cache-missing fallback.
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import CapacityClass, ProbeClass, TaskRequirements
from verdict.orchestration.eligibility import (
    _CAPACITY_ORDER,
    _WORKER_CAPACITY_ORDER,
    EligibilityLadder,
)
from verdict.orchestration.health_cache import (
    CATEGORY_OK,
    FRESH_SECONDS,
    STATE_FRESH,
    STATE_STALE,
    USABLE_SECONDS,
    HealthCache,
    HealthEntry,
    ProbeResult,
)
from verdict.prove_at_rest import (
    AGENTIC_PROBE_EXPECTED,
    AGENTIC_PROBE_FILE,
    ProbeExchange,
    score_agentic_probe,
)
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
WORKER_REQ = TaskRequirements()  # frontier_worthy=False, coding=True
FRONTIER_REQ = TaskRequirements(frontier_worthy=True, max_capability_tier=3)


def _row(
    route_id: str,
    owned_by: str | None = None,
    *,
    pricing: Mapping[str, float] | None = None,
    context: int = 200_000,
) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": context,
        "max_input_tokens": context,
        "max_output_tokens": 32_000,
        "capabilities": {"tool_calling": True, "reasoning": True},
        "pricing": dict(pricing) if pricing is not None else {"input": 1.0, "output": 2.0},
    }


def _conn(
    provider: str, *, auth: str = "oauth", plan: str = "max", free_only: bool = False
) -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": auth,
        "isActive": True,
        "testStatus": "ok",
        "backoffLevel": 0,
        "plan_label": plan,
        "import_free_only": free_only,
    }


class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, route_id: str) -> HealthResult:
        self.calls.append(route_id)
        return HealthResult(healthy=True, category="")


def _make(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    health_cache: HealthCache | None = None,
    allow_unknown: bool | None = None,
    **kwargs: Any,
) -> tuple[EligibilityLadder, FakeProbe]:
    probe = FakeProbe()
    ladder = EligibilityLadder(
        rows,
        connections,
        probe,
        tmp_path / "state.json",
        health_cache=health_cache,
        allow_unknown_capacity=allow_unknown,
        **kwargs,
    )
    return ladder, probe


def _fresh_entry(
    route_id: str,
    *,
    agentic_ok: bool = False,
    probe_class: str = "single_call",
    age_seconds: float = 60.0,
    agentic_checked_at: datetime | None = None,
) -> HealthEntry:
    checked = NOW - timedelta(seconds=age_seconds)
    ack = agentic_checked_at
    if ack is None and agentic_ok:
        ack = checked  # default to checked_at for convenience
    return HealthEntry(
        route_id=route_id,
        category=CATEGORY_OK,
        checked_at=checked,
        until=checked + timedelta(seconds=USABLE_SECONDS),
        consecutive_failures=0,
        chat_ok=True,
        tool_ok=True,
        healthy=True,
        probe_class=probe_class,
        agentic_ok=agentic_ok,
        agentic_checked_at=ack,
    )


def _build_cache(tmp_path: Path, entries: list[HealthEntry]) -> HealthCache:
    cache = HealthCache(tmp_path / "health-cache.json")
    for entry in entries:
        result = ProbeResult(
            category=entry.category,
            chat_ok=entry.chat_ok,
            tool_ok=entry.tool_ok,
            probe_class=entry.probe_class,
            agentic_ok=entry.agentic_ok,
        )
        cache.record(entry.route_id, result, entry.checked_at)
    cache.save()
    return cache


# ---- AC1: Implementation-worker order: FREE > SUBSCRIPTION > METERED -----


class TestCapacityOrdering:
    def test_worker_capacity_order(self) -> None:
        """Free-first order for workers."""
        assert _WORKER_CAPACITY_ORDER[CapacityClass.FREE] == 0
        assert _WORKER_CAPACITY_ORDER[CapacityClass.SUBSCRIPTION] == 1
        assert _WORKER_CAPACITY_ORDER[CapacityClass.METERED] == 2
        assert _WORKER_CAPACITY_ORDER[CapacityClass.UNKNOWN] == 3

    def test_planning_capacity_order(self) -> None:
        """Subscription-first order for planning/review."""
        assert _CAPACITY_ORDER[CapacityClass.SUBSCRIPTION] == 0
        assert _CAPACITY_ORDER[CapacityClass.FREE] == 1
        assert _CAPACITY_ORDER[CapacityClass.METERED] == 2

    def test_free_ranks_first_for_worker_task(self, tmp_path: Path) -> None:
        rows = [
            _row("op/model-a", owned_by="openrouter"),
            _row("gl/model-b", owned_by="glm", pricing={"input": 0.0, "output": 0.0}),
            _row("cc/model-c", owned_by="claude"),
        ]
        conns = [
            _conn("openrouter", auth="apikey", plan="payg"),
            _conn("glm", auth="apikey", plan="free", free_only=True),
            _conn("claude", auth="oauth", plan="max"),
        ]
        cache = _build_cache(
            tmp_path, [_fresh_entry("gl/model-b", agentic_ok=True, probe_class="agentic")]
        )
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
        assert ranked[0].route_id == "gl/model-b"
        assert ranked[0].capacity_class == CapacityClass.FREE

    def test_subscription_ranks_first_for_frontier_task(self, tmp_path: Path) -> None:
        rows = [
            _row("op/model-a", owned_by="openrouter"),
            _row("gl/model-b", owned_by="glm", pricing={"input": 0.0, "output": 0.0}),
            _row("cc/model-c", owned_by="claude"),
        ]
        conns = [
            _conn("openrouter", auth="apikey", plan="payg"),
            _conn("glm", auth="apikey", plan="free", free_only=True),
            _conn("claude", auth="oauth", plan="max"),
        ]
        ladder, _ = _make(tmp_path, rows, conns)
        verdicts = ladder.evaluate(FRONTIER_REQ, now=NOW)
        ranked = sorted((v for v in verdicts if v.rank is not None), key=lambda v: v.rank or 0)
        assert ranked[0].route_id == "cc/model-c"
        assert ranked[0].capacity_class == CapacityClass.SUBSCRIPTION


# ---- AC2: Agentic gate -----


class TestAgenticGate:
    def test_free_with_agentic_pass_is_worker(self, tmp_path: Path) -> None:
        """A FREE route with agentic probe PASS is eligible as implementation worker."""
        entry = _fresh_entry("free/model-a", agentic_ok=True, probe_class="agentic")
        cache = _build_cache(tmp_path, [entry])
        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/model-a"].rank is not None  # ranked = eligible

    def test_free_with_single_call_only_blocked_for_worker(self, tmp_path: Path) -> None:
        """A FREE route with only single-call probe is NOT an implementation worker."""
        entry = _fresh_entry("free/model-a", agentic_ok=False, probe_class="single_call")
        cache = _build_cache(tmp_path, [entry])
        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"

    def test_free_without_cache_entry_blocked_for_worker(self, tmp_path: Path) -> None:
        """A FREE route with no cache entry is blocked when health_cache is present."""
        cache = HealthCache(tmp_path / "health-cache.json")
        cache.save()
        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/model-a"].reason == "no_agentic_probe"

    def test_free_without_health_cache_is_blocked(self, tmp_path: Path) -> None:
        """Without a health_cache attached, FREE routes are NOT implementation-eligible."""
        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=None)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_health_cache"

    def test_subscription_not_gated_by_agentic(self, tmp_path: Path) -> None:
        """Subscription routes bypass the agentic gate entirely."""
        cache = HealthCache(tmp_path / "health-cache.json")
        cache.save()
        rows = [_row("cc/model-a", owned_by="claude")]
        conns = [_conn("claude", auth="oauth", plan="max")]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["cc/model-a"].rank is not None


# ---- AC3: UNKNOWN opt-in -----


class TestUnknownOptIn:
    def test_unknown_blocked_by_default_for_workers(self, tmp_path: Path) -> None:
        """UNKNOWN capacity: auth=apikey with no pricing -> UNKNOWN class."""
        rows = [_row("unk/model-a", owned_by="unknown_provider", pricing=None)]
        # Remove pricing from row to get UNKNOWN capacity
        rows[0].pop("pricing", None)
        conns = [_conn("unknown_provider", auth="apikey", plan="")]
        ladder, _ = _make(tmp_path, rows, conns, allow_unknown=False)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["unk/model-a"].reason == "unknown_capacity_not_opted_in"

    def test_unknown_allowed_with_opt_in(self, tmp_path: Path) -> None:
        rows = [_row("unk/model-a", owned_by="unknown_provider", pricing=None)]
        rows[0].pop("pricing", None)
        conns = [_conn("unknown_provider", auth="apikey", plan="")]
        ladder, _ = _make(tmp_path, rows, conns, allow_unknown=True)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["unk/model-a"].rank is not None

    def test_unknown_allowed_for_frontier(self, tmp_path: Path) -> None:
        """Frontier-worthy tasks always allow UNKNOWN."""
        rows = [_row("unk/model-a", owned_by="unknown_provider", pricing=None)]
        rows[0].pop("pricing", None)
        conns = [_conn("unknown_provider", auth="apikey", plan="")]
        ladder, _ = _make(tmp_path, rows, conns, allow_unknown=False)
        verdicts = ladder.evaluate(FRONTIER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["unk/model-a"].rank is not None


# ---- AC4: Determinism -----


class TestDeterminism:
    def test_same_inputs_same_choice(self, tmp_path: Path) -> None:
        rows = [
            _row("a/model-1", owned_by="a"),
            _row("b/model-2", owned_by="b", pricing={"input": 0, "output": 0}),
        ]
        conns = [_conn("a"), _conn("b", plan="free", free_only=True)]
        ladder1, _ = _make(tmp_path / "r1", rows, conns)
        ladder2, _ = _make(tmp_path / "r2", rows, conns)
        s1, _v1 = ladder1.select(WORKER_REQ, now=NOW)
        s2, _v2 = ladder2.select(WORKER_REQ, now=NOW)
        assert s1 is not None and s2 is not None
        assert s1.route_id == s2.route_id


# ---- AC5: Stale vs fresh -----


class TestStaleFresh:
    def test_fresh_cache_entry_preserves_agentic(self, tmp_path: Path) -> None:
        entry = _fresh_entry("free/m", agentic_ok=True, probe_class="agentic", age_seconds=60)
        cache = _build_cache(tmp_path, [entry])
        lookup = cache.lookup("free/m", NOW)
        assert lookup.state == STATE_FRESH
        assert lookup.entry is not None
        assert lookup.entry.agentic_ok is True

    def test_stale_entry_rejected_by_agentic_gate(self, tmp_path: Path) -> None:
        """Stale agentic entry (agentic_checked_at past FRESH_SECONDS) is rejected."""
        entry = _fresh_entry(
            "free/m", agentic_ok=True, probe_class="agentic", age_seconds=FRESH_SECONDS + 60
        )
        cache = _build_cache(tmp_path, [entry])
        rows = [_row("free/m", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/m"].reason == "agentic_probe_stale"

    def test_expired_entry_fails_agentic_gate(self, tmp_path: Path) -> None:
        """Expired (past usable window) loses agentic qualification."""
        entry = _fresh_entry(
            "free/m", agentic_ok=True, probe_class="agentic", age_seconds=USABLE_SECONDS + 60
        )
        cache = _build_cache(tmp_path, [entry])
        rows = [_row("free/m", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/m"].reason == "agentic_probe_stale"


# ---- AC6: Cache-missing fallback -----


class TestCacheMissing:
    def test_no_health_cache_blocks_free(self, tmp_path: Path) -> None:
        """Without health_cache, FREE routes are NOT implementation-eligible."""
        rows = [
            _row("cc/model-a", owned_by="claude"),
            _row("free/model-b", owned_by="free", pricing={"input": 0, "output": 0}),
        ]
        conns = [_conn("claude"), _conn("free", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=None)
        s, vs = ladder.select(WORKER_REQ, now=NOW)
        # Without cache: free is blocked; subscription route is selected
        assert s is not None
        assert s.route_id == "cc/model-a"
        by_id = {v.route_id: v for v in vs}
        assert by_id["free/model-b"].reason == "no_health_cache"


# ---- AC7: Agentic probe scoring -----


class TestAgenticProbeScoring:
    """AC7: score_agentic_probe validates arguments, not just tool names."""

    def _exchange(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        body_extra: dict[str, Any] | None = None,
    ) -> ProbeExchange:
        args = arguments if arguments is not None else {}
        body: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": tool_name, "arguments": _json.dumps(args)}}
                        ]
                    }
                }
            ]
        }
        if body_extra:
            body.update(body_extra)
        return ProbeExchange(http_status=200, ok=True, response_body=body)

    def _perfect_exchanges(self) -> list[ProbeExchange]:
        """Three-turn probe with correct arguments and edited content."""
        t1 = self._exchange("verdict_probe_read_file", {"path": AGENTIC_PROBE_FILE})
        t2 = self._exchange(
            "verdict_probe_edit_file",
            {"path": AGENTIC_PROBE_FILE, "old_text": "line two", "new_text": "LINE TWO"},
        )
        # Turn 3: read_file again; response body must contain edited content.
        t3_body = {
            "choices": [
                {
                    "message": {
                        "content": AGENTIC_PROBE_EXPECTED,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "verdict_probe_read_file",
                                    "arguments": _json.dumps({"path": AGENTIC_PROBE_FILE}),
                                }
                            }
                        ],
                    }
                }
            ]
        }
        t3 = ProbeExchange(http_status=200, ok=True, response_body=t3_body)
        return [t1, t2, t3]

    def test_perfect_3_turn_passes(self) -> None:
        assert score_agentic_probe(self._perfect_exchanges()) is True

    def test_empty_arguments_fails(self) -> None:
        """Empty {} arguments → FAIL (no path in turn 1)."""
        exchanges = [
            self._exchange("verdict_probe_read_file", {}),
            self._exchange("verdict_probe_edit_file", {}),
            self._exchange("verdict_probe_read_file", {}),
        ]
        assert score_agentic_probe(exchanges) is False

    def test_wrong_old_text_fails(self) -> None:
        """Wrong old_text in turn 2 → FAIL."""
        t1 = self._exchange("verdict_probe_read_file", {"path": AGENTIC_PROBE_FILE})
        t2 = self._exchange(
            "verdict_probe_edit_file",
            {"path": AGENTIC_PROBE_FILE, "old_text": "WRONG", "new_text": "LINE TWO"},
        )
        t3_body = {
            "choices": [
                {
                    "message": {
                        "content": AGENTIC_PROBE_EXPECTED,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "verdict_probe_read_file",
                                    "arguments": _json.dumps({"path": AGENTIC_PROBE_FILE}),
                                }
                            }
                        ],
                    }
                }
            ]
        }
        t3 = ProbeExchange(http_status=200, ok=True, response_body=t3_body)
        assert score_agentic_probe([t1, t2, t3]) is False

    def test_right_calls_wrong_order_fails(self) -> None:
        """Correct calls in wrong order → FAIL (edit before read)."""
        perfect = self._perfect_exchanges()
        # Swap turn 1 (read) and turn 2 (edit)
        assert score_agentic_probe([perfect[1], perfect[0], perfect[2]]) is False

    def test_wrong_tool_name_fails(self) -> None:
        t1 = self._exchange("verdict_probe_read_file", {"path": AGENTIC_PROBE_FILE})
        t2 = self._exchange("wrong_tool", {"path": AGENTIC_PROBE_FILE})
        t3 = self._perfect_exchanges()[2]
        assert score_agentic_probe([t1, t2, t3]) is False

    def test_fewer_than_3_turns_fails(self) -> None:
        perfect = self._perfect_exchanges()
        assert score_agentic_probe(perfect[:2]) is False

    def test_http_error_fails(self) -> None:
        bad = ProbeExchange(http_status=500, ok=False)
        perfect = self._perfect_exchanges()
        assert score_agentic_probe([perfect[0], bad, perfect[2]]) is False

    def test_ok_false_without_status_code_fails(self) -> None:
        """A turn with ok=False but no status_code still fails (Finding 3)."""
        bad = ProbeExchange(http_status=None, ok=False)
        perfect = self._perfect_exchanges()
        assert score_agentic_probe([bad, perfect[1], perfect[2]]) is False

    def test_wrong_edit_path_fails(self) -> None:
        """Edit targeting a different file → FAIL (Finding 3)."""
        t1 = self._exchange("verdict_probe_read_file", {"path": AGENTIC_PROBE_FILE})
        t2 = self._exchange(
            "verdict_probe_edit_file",
            {"path": "/tmp/wrong_file.txt", "old_text": "line two", "new_text": "LINE TWO"},
        )
        t3 = self._perfect_exchanges()[2]
        assert score_agentic_probe([t1, t2, t3]) is False

    def test_turn3_text_is_not_required_final_file_state_is(self, tmp_path: Path) -> None:
        """The edited text comes from the simulated read AFTER turn 3's reply.

        So the scorer must not look for it in the reply body; the prober checks
        the fake file's final state instead. A loop whose edit does not apply
        (wrong old_text) must still fail on that final-state check.
        """
        t1 = self._exchange("verdict_probe_read_file", {"path": AGENTIC_PROBE_FILE})
        t2 = self._exchange(
            "verdict_probe_edit_file",
            {"path": AGENTIC_PROBE_FILE, "old_text": "line two", "new_text": "LINE TWO"},
        )
        t3 = self._exchange("verdict_probe_read_file", {"path": AGENTIC_PROBE_FILE})
        assert score_agentic_probe([t1, t2, t3]) is True

        from verdict.prove_at_rest import AgenticFakeFile

        untouched = AgenticFakeFile()
        assert untouched.edit(AGENTIC_PROBE_FILE, "line 2", "LINE TWO") is False
        assert untouched.content != AGENTIC_PROBE_EXPECTED


# ---- AC: ProbeClass enum -----


class TestProbeClass:
    def test_values(self) -> None:
        assert ProbeClass.AGENTIC.value == "agentic"
        assert ProbeClass.SINGLE_CALL.value == "single_call"
        assert ProbeClass.NONE.value == "none"


# ---- AC: Health entry persistence of probe_class -----


class TestHealthEntryProbeClass:
    def test_round_trip(self, tmp_path: Path) -> None:
        entry = _fresh_entry("r/m", agentic_ok=True, probe_class="agentic")
        d = entry.to_dict()
        assert d["probe_class"] == "agentic"
        assert d["agentic_ok"] is True
        restored = HealthEntry.from_dict(d)
        assert restored.probe_class == "agentic"
        assert restored.agentic_ok is True

    def test_missing_fields_default(self) -> None:
        """Old cache entries without probe_class default safely."""
        d = {
            "route_id": "r/m",
            "category": "ok",
            "checked_at": "2026-09-29T12:00:00+00:00",
            "until": "2026-09-29T12:30:00+00:00",
            "consecutive_failures": 0,
            "chat_ok": True,
            "tool_ok": True,
            "healthy": True,
        }
        entry = HealthEntry.from_dict(d)
        assert entry.probe_class == "single_call"
        assert entry.agentic_ok is False


# ---- AC: Rank components include probe_class and freshness -----


class TestRankComponentsProbeInfo:
    def test_components_include_cache_fields(self, tmp_path: Path) -> None:
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        # Use real now for checked_at so it's fresh at lookup time
        real_now = _dt.now(_tz.utc)
        checked = real_now - timedelta(seconds=30)
        entry = HealthEntry(
            route_id="free/m",
            category=CATEGORY_OK,
            checked_at=checked,
            until=checked + timedelta(seconds=USABLE_SECONDS),
            consecutive_failures=0,
            chat_ok=True,
            tool_ok=True,
            healthy=True,
            probe_class="agentic",
            agentic_ok=True,
            agentic_checked_at=checked,
        )
        cache = _build_cache(tmp_path, [entry])
        rows = [_row("free/m", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=real_now)
        by_id = {v.route_id: v for v in verdicts}
        v = by_id["free/m"]
        assert v.rank_components is not None
        assert v.rank_components["probe_class"] == "agentic"
        assert v.rank_components["cache_freshness"] in (STATE_FRESH, STATE_STALE)
        assert v.rank_components["cache_checked_at"] is not None


# ---- Finding 2: agentic_checked_at separate from checked_at -----


class TestAgenticCheckedAtSeparation:
    def test_single_call_pass_does_not_refresh_agentic_timestamp(self, tmp_path: Path) -> None:
        """Agentic PASS at T0, single-call PASSes at T0+23h and T0+25h.

        At T0+25h the agentic gate must say stale because agentic_checked_at
        is T0, which is 25 h ago (beyond FRESH_SECONDS = 10 min).
        """

        t0 = NOW - timedelta(hours=25)
        cache = HealthCache(tmp_path / "hc.json")
        # Step 1: agentic PASS at T0
        agentic_result = ProbeResult(
            category=CATEGORY_OK, chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        )
        cache.record("free/m", agentic_result, t0)
        entry = cache.entry("free/m")
        assert entry is not None
        assert entry.agentic_ok is True
        assert entry.agentic_checked_at == t0

        # Step 2: single-call PASS at T0+23h
        t_23h = t0 + timedelta(hours=23)
        single_result = ProbeResult(
            category=CATEGORY_OK,
            chat_ok=True,
            tool_ok=True,
            probe_class="single_call",
            agentic_ok=False,
        )
        cache.record("free/m", single_result, t_23h)
        entry2 = cache.entry("free/m")
        assert entry2 is not None
        assert entry2.agentic_ok is True  # preserved
        assert entry2.checked_at == t_23h  # single-call time
        assert entry2.agentic_checked_at == t0  # NOT refreshed

        # Step 3: single-call PASS at T0+25h (= NOW)
        cache.record("free/m", single_result, NOW)
        entry3 = cache.entry("free/m")
        assert entry3 is not None
        assert entry3.agentic_ok is True  # still preserved
        assert entry3.checked_at == NOW  # single-call time
        assert entry3.agentic_checked_at == t0  # NOT refreshed

        cache.save()

        # Step 4: eligibility gate at NOW should reject (agentic_checked_at is 25h old)
        rows = [_row("free/m", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/m"].reason == "agentic_probe_stale"


# ---- Finding 4: wall-time and stop signal in run_agentic_probes -----


class TestAgenticProbeWallTime:
    def test_wall_time_stops_agentic_probes(self) -> None:
        """run_agentic_probes respects the cycle wall-time budget."""

        import tempfile

        from verdict.orchestration.health_cache import HealthCache
        from verdict.prove_at_rest import AdmittedRoute, CycleStats, ProbeExchange, Prober

        with tempfile.TemporaryDirectory() as td:
            cache = HealthCache(Path(td) / "hc.json")
            call_count = 0
            mono_time = [0.0]

            def fake_transport(
                route_id: str, payload: dict[str, Any], timeout: float
            ) -> ProbeExchange:
                nonlocal call_count
                call_count += 1
                # Advance wall time past budget on first call
                mono_time[0] += 700.0
                return ProbeExchange(http_status=200, ok=True, response_body={})

            routes = [
                AdmittedRoute(route_id="free/m1", provider="free", capacity="free"),
                AdmittedRoute(route_id="free/m2", provider="free2", capacity="free"),
            ]
            prober = Prober(
                cache=cache,
                routes_loader=lambda: routes,
                transport=lambda *a: ProbeExchange(http_status=200, ok=True),
                max_wall_seconds=600.0,
                agentic_transport=fake_transport,
                monotonic=lambda: mono_time[0],
            )
            stats = CycleStats()
            prober.run_agentic_probes(stats, started=0.0)
            # Should have stopped after first route's first turn (wall cap exceeded)
            assert call_count <= 3  # at most one route's 3 turns

    def test_stop_signal_stops_agentic_probes(self) -> None:
        """run_agentic_probes respects the stop signal."""
        import tempfile

        from verdict.orchestration.health_cache import HealthCache
        from verdict.prove_at_rest import AdmittedRoute, CycleStats, ProbeExchange, Prober

        with tempfile.TemporaryDirectory() as td:
            cache = HealthCache(Path(td) / "hc.json")
            call_count = 0

            def fake_transport(
                route_id: str, payload: dict[str, Any], timeout: float
            ) -> ProbeExchange:
                nonlocal call_count
                call_count += 1
                return ProbeExchange(http_status=200, ok=True, response_body={})

            routes = [
                AdmittedRoute(route_id="free/m1", provider="free", capacity="free"),
                AdmittedRoute(route_id="free/m2", provider="free2", capacity="free"),
            ]
            prober = Prober(
                cache=cache,
                routes_loader=lambda: routes,
                transport=lambda *a: ProbeExchange(http_status=200, ok=True),
                agentic_transport=fake_transport,
            )
            prober.stop()  # set the stop signal
            stats = CycleStats()
            prober.run_agentic_probes(stats, started=0.0)
            assert call_count == 0  # no probes should have run


# ---- Finding 6: receipt probe-class fields -----


class TestReceiptProbeFields:
    def test_selection_event_carries_probe_fields(self) -> None:
        """Probe-class fields reach the receipt through the selection event."""
        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.receipt import _node_record

        events = [
            RunEvent(
                seq=1,
                at="2026-09-29T12:00:00+00:00",
                type="selection",
                node_id="worker-0",
                data={
                    "route_id": "free/m",
                    "provider": "free",
                    "capacity_class": "free",
                    "probe_class": "agentic",
                    "cache_checked_at": "2026-09-29T11:59:00+00:00",
                    "cache_freshness": "fresh",
                },
            ),
            RunEvent(
                seq=2,
                at="2026-09-29T12:01:00+00:00",
                type="terminal",
                node_id="worker-0",
                data={"route_id": "free/m", "ok": True, "duration_seconds": 5.0},
            ),
        ]
        record = _node_record("worker-0", "implementation", events)
        # The attempt row should carry probe fields from the selection event.
        attempts = record.get("attempts", [])
        assert len(attempts) >= 1
        row = attempts[0]
        assert row.get("probe_class") == "agentic"
        assert row.get("cache_checked_at") == "2026-09-29T11:59:00+00:00"
        assert row.get("cache_freshness") == "fresh"


# ---- Finding 1: CAPACITY_CLASS_ORDER unchanged from main ----


class TestCapacityClassOrderGlobal:
    def test_global_order_is_subscription_first(self) -> None:
        """CAPACITY_CLASS_ORDER in subagent_selection is subscription-first (main).

        Free-first is scoped to the EligibilityLadder, not to the global
        ranking used by the non-ladder selection path.
        """
        from verdict.subagent_selection import CAPACITY_CLASS_ORDER

        assert CAPACITY_CLASS_ORDER == ("claude_subscription", "subscription", "free", "metered")


# ---- Finding 2: Simulated file tool loop ----


class TestSimulatedFileToolLoop:
    """The 3-turn agentic loop must run a real simulated tool loop."""

    def test_model_echoing_content_without_edit_fails(self) -> None:
        """A model that echoes LINE TWO without calling edit_file → FAIL."""
        from verdict.prove_at_rest import (
            AGENTIC_PROBE_ORIGINAL,
            AgenticFakeFile,
            _extract_tool_calls,
            _simulate_tool_calls,
        )

        fake = AgenticFakeFile()
        assert fake.content == AGENTIC_PROBE_ORIGINAL
        # Model echoes the expected content but makes no tool calls.
        response_body: dict[str, Any] = {
            "choices": [{"message": {"content": AGENTIC_PROBE_EXPECTED}}]
        }
        calls = _extract_tool_calls(response_body)
        assert len(calls) == 0
        results = _simulate_tool_calls(fake, calls)
        assert len(results) == 0
        # File content unchanged.
        assert fake.content == AGENTIC_PROBE_ORIGINAL

    def test_correct_tool_calls_modify_fake_file(self) -> None:
        """Correct read/edit/read sequence modifies the fake file correctly."""
        from verdict.prove_at_rest import (
            AGENTIC_PROBE_ORIGINAL,
            AGENTIC_TOOL_EDIT,
            AGENTIC_TOOL_READ,
            AgenticFakeFile,
            _extract_tool_calls,
            _simulate_tool_calls,
        )

        fake = AgenticFakeFile()

        # Turn 1: read
        read_body: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "tc1",
                                "function": {
                                    "name": AGENTIC_TOOL_READ,
                                    "arguments": _json.dumps({"path": AGENTIC_PROBE_FILE}),
                                },
                            }
                        ]
                    }
                }
            ]
        }
        calls = _extract_tool_calls(read_body)
        results = _simulate_tool_calls(fake, calls)
        assert len(results) == 1
        assert results[0]["content"] == AGENTIC_PROBE_ORIGINAL

        # Turn 2: edit
        edit_body: dict[str, Any] = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "tc2",
                                "function": {
                                    "name": AGENTIC_TOOL_EDIT,
                                    "arguments": _json.dumps(
                                        {
                                            "path": AGENTIC_PROBE_FILE,
                                            "old_text": "line two",
                                            "new_text": "LINE TWO",
                                        }
                                    ),
                                },
                            }
                        ]
                    }
                }
            ]
        }
        calls = _extract_tool_calls(edit_body)
        results = _simulate_tool_calls(fake, calls)
        assert len(results) == 1
        assert _json.loads(results[0]["content"])["ok"] is True
        assert fake.content == AGENTIC_PROBE_EXPECTED

    def test_wrong_path_edit_fails(self) -> None:
        """An edit targeting a wrong path does not modify the fake file."""
        from verdict.prove_at_rest import (
            AGENTIC_PROBE_ORIGINAL,
            AgenticFakeFile,
            _simulate_tool_calls,
        )

        fake = AgenticFakeFile()
        calls = [
            {
                "id": "tc1",
                "name": "verdict_probe_edit_file",
                "arguments": {
                    "path": "/tmp/wrong_file.txt",
                    "old_text": "line two",
                    "new_text": "LINE TWO",
                },
            }
        ]
        results = _simulate_tool_calls(fake, calls)
        assert _json.loads(results[0]["content"])["ok"] is False
        assert fake.content == AGENTIC_PROBE_ORIGINAL


# ---- Finding 4: _needs_agentic uses agentic_checked_at ----


class TestNeedsAgenticUsesAgenticTimestamp:
    def test_single_call_refresh_does_not_starve_agentic_reprobe(self, tmp_path: Path) -> None:
        """Single-call probes refresh checked_at but NOT agentic_checked_at.

        Therefore _needs_agentic must use agentic_checked_at, not checked_at.
        If it used checked_at, repeated single-call probes would prevent the
        scheduled agentic re-probe from ever running.
        """
        from verdict.prove_at_rest import AdmittedRoute, Prober

        cache = HealthCache(tmp_path / "hc.json")

        # Agentic PASS at T0 = NOW - 25h.
        t0 = NOW - timedelta(hours=25)
        agentic_result = ProbeResult(
            category=CATEGORY_OK, chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        )
        cache.record("free/m", agentic_result, t0)

        # Single-call PASS at NOW (recent, but should not help agentic).
        single_result = ProbeResult(
            category=CATEGORY_OK,
            chat_ok=True,
            tool_ok=True,
            probe_class="single_call",
            agentic_ok=False,
        )
        cache.record("free/m", single_result, NOW)

        entry = cache.entry("free/m")
        assert entry is not None
        assert entry.checked_at == NOW  # recent single-call
        assert entry.agentic_checked_at == t0  # old agentic

        route = AdmittedRoute(route_id="free/m", provider="free", capacity="free")
        prober = Prober(
            cache=cache,
            routes_loader=lambda: [],
            transport=lambda *a: ProbeExchange(http_status=200, ok=True),
        )
        # 24-hour interval: agentic was 25h ago, so it should be stale.
        assert prober._needs_agentic(route, NOW) is True

        # If agentic probe was recent (1h ago), it should NOT need reprobe.
        recent = NOW - timedelta(hours=1)
        cache.record("free/m", agentic_result, recent)
        assert prober._needs_agentic(route, NOW) is False


# ---- Finding 5: Per-turn bucket consumption ----


class TestPerTurnBucketConsumption:
    def test_bucket_exhaustion_records_not_tested(self) -> None:
        """When the bucket runs out mid-probe, record 'not tested' not failure."""
        import tempfile

        from verdict.orchestration.health_cache import TokenBucket
        from verdict.prove_at_rest import AdmittedRoute, CycleStats, Prober

        with tempfile.TemporaryDirectory() as td:
            cache = HealthCache(Path(td) / "hc.json")
            call_count = 0

            # Set up a bucket with only 2 tokens (enough for turn 1 consume
            # but not turn 2). We pre-fill the bucket with timestamps to
            # make remaining=2 out of capacity 3.
            bucket = cache.bucket_for("free")
            # Use a bucket with capacity 2 so the first consume passes,
            # second consume passes, but the third fails.
            cache._buckets["free"] = TokenBucket(
                capacity=2, window_seconds=bucket.window_seconds, timestamps=[]
            )

            def fake_transport(
                route_id: str, payload: dict[str, Any], timeout: float
            ) -> ProbeExchange:
                nonlocal call_count
                call_count += 1
                return ProbeExchange(
                    http_status=200,
                    ok=True,
                    response_body={"choices": [{"message": {"content": "ok"}}]},
                )

            routes = [AdmittedRoute(route_id="free/m1", provider="free", capacity="free")]
            prober = Prober(
                cache=cache,
                routes_loader=lambda: routes,
                transport=lambda *a: ProbeExchange(http_status=200, ok=True),
                agentic_transport=fake_transport,
            )
            stats = CycleStats()
            prober.run_agentic_probes(stats, started=0.0)

            # With capacity=2, turns 1 and 2 can consume tokens but turn 3
            # cannot. Budget exhausted → not tested, no agentic_fail recorded.
            entry = cache.entry("free/m1")
            # Should either not exist or not be an agentic failure.
            if entry is not None:
                assert entry.category != "agentic_fail"


# ---- Finding 6: UNKNOWN opt-in in selection event and receipt ----


class TestUnknownOptInRecording:
    def test_selection_event_carries_opt_in_flag(self) -> None:
        """Selection event includes unknown_capacity_opt_in when selecting UNKNOWN."""
        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.receipt import _node_record

        events = [
            RunEvent(
                seq=1,
                at="2026-09-29T12:00:00+00:00",
                type="selection",
                node_id="worker-0",
                data={
                    "route_id": "free/m",
                    "provider": "free",
                    "capacity_class": "unknown",
                    "unknown_capacity_opt_in": True,
                    "probe_class": "agentic",
                    "cache_checked_at": "2026-09-29T11:59:00+00:00",
                    "cache_freshness": "fresh",
                },
            ),
            RunEvent(
                seq=2,
                at="2026-09-29T12:00:01+00:00",
                type="dispatch",
                node_id="worker-0",
                data={"route_id": "free/m"},
            ),
            RunEvent(
                seq=3,
                at="2026-09-29T12:01:00+00:00",
                type="terminal",
                node_id="worker-0",
                data={"route_id": "free/m", "ok": True, "duration_seconds": 5.0},
            ),
        ]
        record = _node_record("worker-0", "implementation", events)
        attempts = record.get("attempts", [])
        assert len(attempts) >= 1
        row = attempts[0]
        assert row.get("unknown_capacity_opt_in") == "true"

    def test_non_unknown_does_not_carry_opt_in(self) -> None:
        """Selection of a non-UNKNOWN route does NOT carry opt-in."""
        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.receipt import _node_record

        events = [
            RunEvent(
                seq=1,
                at="2026-09-29T12:00:00+00:00",
                type="selection",
                node_id="worker-0",
                data={"route_id": "cc/model", "provider": "cc", "capacity_class": "subscription"},
            ),
            RunEvent(
                seq=2,
                at="2026-09-29T12:00:01+00:00",
                type="dispatch",
                node_id="worker-0",
                data={"route_id": "cc/model"},
            ),
            RunEvent(
                seq=3,
                at="2026-09-29T12:01:00+00:00",
                type="terminal",
                node_id="worker-0",
                data={"route_id": "cc/model", "ok": True, "duration_seconds": 5.0},
            ),
        ]
        record = _node_record("worker-0", "implementation", events)
        attempts = record.get("attempts", [])
        assert len(attempts) >= 1
        row = attempts[0]
        assert row.get("unknown_capacity_opt_in") is None


# ---- Finding 7: Integration receipt test (not fabricated) ----


class TestIntegrationReceiptFromLadder:
    def test_ladder_selection_to_receipt(self, tmp_path: Path) -> None:
        """End-to-end: ladder selects, runtime emits events, receipt has probe fields."""
        # Build a ladder with health cache containing agentic probe data.
        cache = HealthCache(tmp_path / "hc.json")
        agentic_result = ProbeResult(
            category=CATEGORY_OK, chat_ok=True, tool_ok=True, probe_class="agentic", agentic_ok=True
        )
        cache.record("free/m", agentic_result, NOW)
        cache.save()

        rows = [_row("free/m", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        # Select from the ladder.
        choice, _verdicts = ladder.select(WORKER_REQ, now=NOW)
        assert choice is not None
        assert choice.route_id == "free/m"
        assert choice.rank_components is not None
        assert choice.rank_components.get("probe_class") == "agentic"
        assert choice.rank_components.get("cache_freshness") == STATE_FRESH

        # Simulate the runtime's event emission.
        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.receipt import _node_record

        _probe_fields: dict[str, Any] = {}
        if choice.rank_components:
            for pf in ("probe_class", "cache_checked_at", "cache_freshness"):
                if choice.rank_components.get(pf) is not None:
                    _probe_fields[pf] = str(choice.rank_components[pf])

        events = [
            RunEvent(
                seq=1,
                at=NOW.isoformat(),
                type="selection",
                node_id="worker-0",
                data={
                    "route_id": choice.route_id,
                    "provider": "free",
                    "capacity_class": choice.capacity_class.value,
                    **_probe_fields,
                },
            ),
            RunEvent(
                seq=2,
                at=NOW.isoformat(),
                type="dispatch",
                node_id="worker-0",
                data={"route_id": choice.route_id},
            ),
            RunEvent(
                seq=3,
                at=(NOW + timedelta(seconds=5)).isoformat(),
                type="terminal",
                node_id="worker-0",
                data={"route_id": choice.route_id, "ok": True, "duration_seconds": 5.0},
            ),
        ]
        record = _node_record("worker-0", "implementation", events)
        attempts = record.get("attempts", [])
        assert len(attempts) >= 1
        row = attempts[0]
        assert row.get("probe_class") == "agentic"
        assert row.get("cache_freshness") == STATE_FRESH


# ---- Integration review (0.4.0): agentic scoring, identity, retry interval,
# ---- receipt probe buffer, routing --node ----


def _agentic_model(reported: str) -> Any:
    """A scripted model that does a correct read -> edit -> read loop."""
    turn = {"n": 0}

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        turn["n"] += 1
        return {
            "model": reported,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"c{turn['n']}",
                                "type": "function",
                                "function": {"name": name, "arguments": _json.dumps(args)},
                            }
                        ],
                    }
                }
            ],
        }

    from verdict.prove_at_rest import AGENTIC_TOOL_EDIT, AGENTIC_TOOL_READ

    script = [
        (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
        (
            AGENTIC_TOOL_EDIT,
            {"path": AGENTIC_PROBE_FILE, "old_text": "line two", "new_text": "LINE TWO"},
        ),
        (AGENTIC_TOOL_READ, {"path": AGENTIC_PROBE_FILE}),
    ]

    def transport(route_id: str, payload: dict[str, Any], timeout: float) -> ProbeExchange:
        name, args = script[min(turn["n"], 2)]
        body = call(name, args)
        return ProbeExchange(
            http_status=200, ok=True, response_body=body, reported_model=str(body["model"])
        )

    return transport


def _run_agentic(tmp_path: Path, reported: str) -> Any:
    from verdict.prove_at_rest import AdmittedRoute, CycleStats, Prober

    cache = HealthCache(tmp_path / "hc.json", bucket_capacity=100)
    route = AdmittedRoute(route_id="nvidia/moonshotai/kimi-k3", provider="nvidia", capacity="free")
    prober = Prober(
        cache=cache,
        routes_loader=lambda: [route],
        transport=lambda *a: ProbeExchange(http_status=200, ok=True),
        agentic_transport=_agentic_model(reported),
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
    )
    prober.run_agentic_probes(CycleStats(), started=0.0)
    return cache.entry(route.route_id), prober, route, cache


class TestAgenticIntegrationFixes:
    def test_correct_read_edit_read_loop_passes(self, tmp_path: Path) -> None:
        """A normal loop passes: the edited text is checked on the fake file, not the reply."""
        entry, _, _, _ = _run_agentic(tmp_path, "moonshotai/kimi-k3")
        assert entry is not None
        assert entry.agentic_ok is True, entry
        assert entry.agentic_checked_at == NOW

    def test_other_model_echo_cannot_qualify_the_route(self, tmp_path: Path) -> None:
        entry, _, _, _ = _run_agentic(tmp_path, "other/kimi-k3")
        assert entry is not None
        assert entry.agentic_ok is False
        assert entry.category == "model_mismatch"

    def test_absent_model_echo_cannot_qualify_the_route(self, tmp_path: Path) -> None:
        entry, _, _, _ = _run_agentic(tmp_path, "")
        assert entry is not None
        assert entry.agentic_ok is False

    def test_failed_agentic_probe_waits_for_the_interval(self, tmp_path: Path) -> None:
        entry, prober, route, _ = _run_agentic(tmp_path, "other/kimi-k3")
        assert entry is not None and entry.agentic_checked_at == NOW
        assert prober._needs_agentic(route, NOW + timedelta(hours=1)) is False
        assert prober._needs_agentic(route, NOW + timedelta(hours=25)) is True


class TestReceiptProbeBuffer:
    def test_replaced_selection_does_not_leak_probe_fields(self) -> None:
        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.receipt import _node_record

        def ev(seq: int, typ: str, **data: Any) -> RunEvent:
            return RunEvent(seq=seq, at=NOW.isoformat(), type=typ, node_id="n1", data=data)

        events = [
            ev(1, "selection", route_id="free/a", probe_class="agentic", attempt=1),
            ev(2, "selection", route_id="sub/b", attempt=1),
            ev(3, "dispatch", route_id="sub/b", attempt=1),
            ev(4, "terminal", route_id="sub/b", ok=True, attempt=1),
        ]
        rows = _node_record("n1", "implementation", events)["attempts"]
        row = rows[0]
        assert row["route_id"] == "sub/b"
        assert "probe_class" not in row, f"free/a probe evidence leaked onto sub/b: {row}"

    def test_selection_fields_apply_to_the_same_route(self) -> None:
        from verdict.orchestration.contracts import RunEvent
        from verdict.orchestration.receipt import _node_record

        def ev(seq: int, typ: str, **data: Any) -> RunEvent:
            return RunEvent(seq=seq, at=NOW.isoformat(), type=typ, node_id="n1", data=data)

        events = [
            ev(1, "selection", route_id="free/a", probe_class="agentic", attempt=1),
            ev(2, "dispatch", route_id="free/a", attempt=1),
            ev(3, "terminal", route_id="free/a", ok=True, attempt=1),
        ]
        rows = _node_record("n1", "implementation", events)["attempts"]
        assert rows[0].get("probe_class") == "agentic"
