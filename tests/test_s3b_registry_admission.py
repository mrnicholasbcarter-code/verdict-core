"""Tests for S3b: discovery-registry proof gate in EligibilityLadder.

ACs (story S3b / master plan):
 1. A fresh tool_ok=True registry entry admits a FREE route with no health-cache entry.
 2. An expired entry (> TTL_S from ladder.evaluate now) does not admit.
 3. tool_ok=None / answered=False entries (fixture-/demo-style placeholders) do not admit.
 4. tool_ok=False (measured fail) does not admit.
 5. PRICED and UNKNOWN routes are never promoted via the registry.
 6. The 'omniroute/' gateway prefix is stripped before the registry lookup.
 7. An absent or corrupt registry produces no exception; falls back to no_agentic_probe.
 8. The funnel's registry_proof count is non-None when a registry is passed.

Runtime isolation: conftest._isolate_home sets HOME to a tmp dir and
_isolate_host_state_env clears VERDICT_HOME.  Each test that needs a registry
sets VERDICT_HOME to its own tmp_path via monkeypatch so load_registry() finds
the prepared file.

IMPORTANT: load_registry() checks written_at against the *wall clock*.  Tests
therefore write written_at as datetime.now(UTC).  The entry checked_at is
written relative to NOW (the fixed test epoch 2026-09-29T12:00:00Z) because
tool_ok_proof receives now=NOW from ladder.evaluate(now=NOW).
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest

from verdict.discovery.registry import (
    BREAKER_CLOSED,
    REGISTRY_SCHEMA,
    REGISTRY_TTL_S,
    DiscoveryRegistry,
    RegistryEntry,
    save_registry,
    tool_ok_proof,
)
from verdict.orchestration.contracts import CapacityClass, TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import HealthCache

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NOW = datetime.datetime(2026, 9, 29, 12, 0, 0, tzinfo=datetime.timezone.utc)
WORKER_REQ = TaskRequirements()  # frontier_worthy=False, coding=True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime.datetime:
    """Wall-clock now (tz-aware UTC).  Used for written_at so load_registry passes TTL."""
    return datetime.datetime.now(datetime.timezone.utc)


def _entry(
    route_key: str = "free/model-a",
    *,
    capacity_class: str = "free",
    answered: bool = True,
    tool_ok: bool | None = True,
    served_model: str | None = None,
    checked_at: datetime.datetime | None = None,
) -> RegistryEntry:
    """Build a RegistryEntry with sensible defaults for admission tests."""
    if checked_at is None:
        # Fresh: 1 hour before the ladder's NOW
        checked_at = NOW - datetime.timedelta(hours=1)
    # Derive served_model from route_key if not supplied and tool_ok=True
    if served_model is None and tool_ok is True:
        served_model = route_key.split("/", 1)[1] if "/" in route_key else route_key
    return RegistryEntry(
        route_key=route_key,
        capacity_class=capacity_class,
        capacity_fact="plan_free",
        base_url="https://example.com",
        auth="env:OPENROUTER_API_KEY",
        upstream=None,
        answered=answered,
        tool_ok=tool_ok,
        tool_skip_reason=None,
        served_model=served_model,
        latency_ms=120,
        retry_after_s=None,
        breaker_state=BREAKER_CLOSED,
        checked_at=checked_at.isoformat(),
    )


def _registry(entries: list[RegistryEntry], *, written_at: str | None = None) -> DiscoveryRegistry:
    """Build a DiscoveryRegistry.  written_at defaults to wall-clock now."""
    return DiscoveryRegistry(
        schema=REGISTRY_SCHEMA,
        written_at=written_at or _utcnow().isoformat(),
        ttl_s=REGISTRY_TTL_S,
        entries=entries,
    )


def _write_registry(tmp_path: Path, reg: DiscoveryRegistry) -> None:
    """Write registry to tmp_path/discovery/registry.json via save_registry."""
    env: dict[str, str] = {"VERDICT_HOME": str(tmp_path)}
    save_registry(reg, env=env)


def _row(
    route_id: str, owned_by: str | None = None, *, pricing: dict[str, float] | None = None
) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": 128_000,
        "max_input_tokens": 128_000,
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


class _FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, route_id: str) -> Any:
        from verdict.subagent_selection import HealthResult

        self.calls.append(route_id)
        return HealthResult(healthy=True, category="")


def _make(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    connections: list[dict[str, Any]],
    health_cache: HealthCache | None = None,
) -> tuple[EligibilityLadder, _FakeProbe]:
    probe = _FakeProbe()
    ladder = EligibilityLadder(
        rows, connections, probe, tmp_path / "state.json", health_cache=health_cache
    )
    return ladder, probe


def _empty_cache(tmp_path: Path) -> HealthCache:
    """Return a HealthCache with no entries (attached but empty)."""
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.save()
    return cache


# ---------------------------------------------------------------------------
# AC1: Fresh proof admits
# ---------------------------------------------------------------------------


class TestFreshProofAdmits:
    def test_fresh_registry_proof_admits_free_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A free route with no health-cache entry is admitted when the registry has
        a fresh tool_ok=True proof (AC1)."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        entry = _entry("free/model-a")
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is not None, "registry proof should admit free route"
        assert by_id["free/model-a"].reason != "no_agentic_probe"


# ---------------------------------------------------------------------------
# AC2: Expired entry does not admit
# ---------------------------------------------------------------------------


class TestExpiredEntryDoesNotAdmit:
    def test_entry_older_than_ttl_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An entry checked_at older than REGISTRY_TTL_S before NOW is not proof."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        stale_checked = NOW - datetime.timedelta(seconds=REGISTRY_TTL_S + 1)
        entry = _entry("free/model-a", checked_at=stale_checked)
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"


# ---------------------------------------------------------------------------
# AC3: Fixture / demo row (answered=False, tool_ok=None) does not admit
# ---------------------------------------------------------------------------


class TestFixtureRowDoesNotAdmit:
    def test_not_answered_does_not_admit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """answered=False means no live HTTP 200 was returned — not a valid proof."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        entry = _entry("free/model-a", answered=False, tool_ok=True)
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"

    def test_tool_ok_none_does_not_admit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tool_ok=None means the tool probe was not attempted — not a valid proof."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        entry = _entry("free/model-a", tool_ok=None, served_model=None)
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"

    def test_tool_ok_false_does_not_admit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """tool_ok=False is a measured fail — the registry explicitly contradicts proof."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        entry = _entry("free/model-a", tool_ok=False, served_model="model-a")
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"


# ---------------------------------------------------------------------------
# AC4: Priced and unknown capacity routes are never promoted via registry
# ---------------------------------------------------------------------------


class TestPricedAndUnknownNotPromoted:
    def test_priced_route_unaffected_by_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A metered (PRICED) route is not gated by agentic proof at all and its
        capacity class is never changed by the registry.  The registry entry with
        capacity_class='free' for a priced route is ignored."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        # Registry says free/tool_ok=True — but route has real pricing (PRICED).
        entry = _entry("op/model-a", capacity_class="free", tool_ok=True)
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("op/model-a", owned_by="openrouter", pricing={"input": 1.0, "output": 2.0})]
        conns = [_conn("openrouter", auth="apikey", plan="payg")]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        # Metered route is ranked (no agentic gate) but stays PRICED/METERED.
        assert by_id["op/model-a"].capacity_class != CapacityClass.FREE

    def test_unknown_capacity_not_admitted_via_registry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An UNKNOWN route stays UNKNOWN: registry only helps FREE routes.
        (Rule: unknown stays unknown — a route is FREE only from declared or
        observed facts, never from a registry that cannot prove capacity class.)"""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        # Route has no pricing at all → UNKNOWN.  Registry has tool_ok=True.
        entry = _entry("unk/model-a", capacity_class="free", tool_ok=True)
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        rows = [_row("unk/model-a", owned_by="unk")]
        rows[0].pop("pricing", None)  # Remove pricing → UNKNOWN capacity
        conns = [_conn("unk", auth="apikey", plan="")]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["unk/model-a"].capacity_class == CapacityClass.UNKNOWN
        # UNKNOWN routes without health_cache agentic proof are still blocked.
        assert by_id["unk/model-a"].rank is None


# ---------------------------------------------------------------------------
# AC5: Gateway prefix stripped before lookup
# ---------------------------------------------------------------------------


class TestGatewayPrefixStripped:
    def test_omniroute_prefix_stripped_for_registry_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Route IDs in the ladder may carry an 'omniroute/' prefix; the registry
        key never does.  Stripping ensures the lookup finds the right entry."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        # Registry stores unprefixed key; route will be looked up with prefix.
        entry = _entry("free/model-a")  # unprefixed
        reg = _registry([entry])
        _write_registry(tmp_path, reg)

        # Route row uses the unprefixed id — EligibilityLadder may add prefix
        # internally or accept it without.  We test via tool_ok_proof directly:
        result_prefixed = tool_ok_proof("omniroute/free/model-a", reg, NOW)
        result_plain = tool_ok_proof("free/model-a", reg, NOW)

        assert result_prefixed["qualified"] is True, "prefix should be stripped before lookup"
        assert result_plain["qualified"] is True, "plain id should also qualify"
        assert result_prefixed["reason"] is None
        assert result_plain["reason"] is None


# ---------------------------------------------------------------------------
# AC6: Absent or corrupt registry falls back gracefully
# ---------------------------------------------------------------------------


class TestAbsentOrCorruptRegistryFallback:
    def test_absent_registry_no_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no registry file exists, routes fall back to no_agentic_probe.
        No exception must propagate."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        # Do not write any registry file.

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"

    def test_corrupt_registry_no_exception(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A corrupt registry file (not valid JSON) does not raise; falls back."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))

        reg_dir = tmp_path / "discovery"
        reg_dir.mkdir(parents=True, exist_ok=True)
        (reg_dir / "registry.json").write_text("not valid json!!", encoding="utf-8")

        rows = [_row("free/model-a", owned_by="free", pricing={"input": 0.0, "output": 0.0})]
        conns = [_conn("free", auth="apikey", plan="free", free_only=True)]
        cache = _empty_cache(tmp_path)
        ladder, _ = _make(tmp_path, rows, conns, health_cache=cache)

        verdicts = ladder.evaluate(WORKER_REQ, now=NOW)
        by_id = {v.route_id: v for v in verdicts}

        assert by_id["free/model-a"].rank is None
        assert by_id["free/model-a"].reason == "no_agentic_probe"


# ---------------------------------------------------------------------------
# AC7: Funnel registry_proof count
# ---------------------------------------------------------------------------


class TestFunnelRegistryProofCount:
    def test_funnel_registry_proof_count_appears(self, tmp_path: Path) -> None:
        """When build_funnel is called with a registry argument, the tool_ok
        stage carries a 'registry_proof' key."""
        from verdict.discovery_funnel import build_funnel

        reg = _registry(
            [
                _entry("free/model-a"),
                _entry("free/model-b"),
                _entry("free/model-c", tool_ok=None, served_model=None),
            ]
        )

        result = build_funnel(
            identify_report=None, health_cache=None, admission=None, now=NOW, registry=reg
        )
        tool_ok_stage = result["stages"]["tool_ok"]

        assert "registry_proof" in tool_ok_stage, (
            "tool_ok stage must carry registry_proof when registry is passed"
        )
        assert isinstance(tool_ok_stage["registry_proof"], int)
        # The two entries with tool_ok=True are proved; the None one is not.
        assert tool_ok_stage["registry_proof"] == 2
