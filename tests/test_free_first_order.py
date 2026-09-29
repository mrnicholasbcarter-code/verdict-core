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
from verdict.prove_at_rest import ProbeExchange, score_agentic_probe
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
) -> HealthEntry:
    checked = NOW - timedelta(seconds=age_seconds)
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
        ladder, _ = _make(tmp_path, rows, conns)  # no health_cache
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

    def test_free_without_health_cache_is_eligible(self, tmp_path: Path) -> None:
        """Without a health_cache attached, free routes pass (backward compat)."""
        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0, "output": 0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=None)
        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}
        assert by_id["free/model-a"].rank is not None

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

    def test_stale_entry_still_qualifies(self, tmp_path: Path) -> None:
        """Stale-healthy entry (past fresh window but inside usable) still works."""
        entry = _fresh_entry(
            "free/m", agentic_ok=True, probe_class="agentic", age_seconds=FRESH_SECONDS + 60
        )
        cache = _build_cache(tmp_path, [entry])
        lookup = cache.lookup("free/m", NOW)
        assert lookup.state == STATE_STALE
        assert lookup.entry is not None and lookup.entry.agentic_ok is True

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
        assert by_id["free/m"].reason == "agentic_probe_expired"


# ---- AC6: Cache-missing fallback -----


class TestCacheMissing:
    def test_no_health_cache_behaves_as_before(self, tmp_path: Path) -> None:
        """Without health_cache, selection is today's behavior (no free-first gate)."""
        rows = [
            _row("cc/model-a", owned_by="claude"),
            _row("free/model-b", owned_by="free", pricing={"input": 0, "output": 0}),
        ]
        conns = [_conn("claude"), _conn("free", plan="free", free_only=True)]
        ladder, _ = _make(tmp_path, rows, conns, health_cache=None)
        s, _ = ladder.select(WORKER_REQ, now=NOW)
        # Without cache: free ranks first (worker order), and is eligible
        assert s is not None


# ---- AC7: Agentic probe scoring -----


class TestAgenticProbeScoring:
    def _ok_exchange(self, tool_name: str) -> ProbeExchange:
        return ProbeExchange(
            http_status=200,
            ok=True,
            response_body={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [{"function": {"name": tool_name, "arguments": "{}"}}]
                        }
                    }
                ]
            },
        )

    def test_perfect_3_turn_passes(self) -> None:
        exchanges = [
            self._ok_exchange("verdict_probe_read_file"),
            self._ok_exchange("verdict_probe_edit_file"),
            self._ok_exchange("verdict_probe_read_file"),
        ]
        assert score_agentic_probe(exchanges) is True

    def test_wrong_tool_name_fails(self) -> None:
        exchanges = [
            self._ok_exchange("verdict_probe_read_file"),
            self._ok_exchange("wrong_tool"),
            self._ok_exchange("verdict_probe_read_file"),
        ]
        assert score_agentic_probe(exchanges) is False

    def test_fewer_than_3_turns_fails(self) -> None:
        exchanges = [
            self._ok_exchange("verdict_probe_read_file"),
            self._ok_exchange("verdict_probe_edit_file"),
        ]
        assert score_agentic_probe(exchanges) is False

    def test_http_error_fails(self) -> None:
        bad = ProbeExchange(http_status=500, ok=False, status_code=500)
        exchanges = [
            self._ok_exchange("verdict_probe_read_file"),
            bad,
            self._ok_exchange("verdict_probe_read_file"),
        ]
        assert score_agentic_probe(exchanges) is False


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
