"""Red tests for BOD-418 openrouter free-set gaps (G1-G5).

These tests FAIL on origin/main 6ed4f21a. Each gap has one failing test.
Workers implement the fix and make their test(s) pass.

G1: EligibilityLadder.select() cools pool/provider on a paid openrouter key-limit 403.
G2: omniroute_evidence.parse_omniroute_evidence passes openrouter_free_set=None.
G3: verified_models._capacity_for_row passes openrouter_free_set=None.
G4: autodev_run trusts route_id.endswith(":free") instead of capacity class only.
G5: acceptance does not refresh the openrouter free-set cache.
G6: Stale ":free" id that 404s "unavailable for free" has no named route-scoped drop test.

Run: TMPDIR=/tmp uv run python -m pytest -q -p no:cacheprovider tests/test_bod418_gaps.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.capacity import CapacityClass
from verdict.openrouter_free_set import (
    DEFAULT_TTL_SECONDS,
    OpenRouterFreeSet,
    key_limit_on_non_free_is_route_scoped,
    openrouter_route_is_public_free,
    parse_free_entries,
)
from verdict.orchestration.contracts import TaskRequirements
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.health_cache import (
    CATEGORY_NOT_FOUND,
    CATEGORY_OK,
    HealthCache,
    ProbeResult,
)
from verdict.subagent_selection import HealthResult

NOW = datetime(2026, 11, 14, 10, 0, tzinfo=timezone.utc)
REQ = TaskRequirements(
    required_capabilities=frozenset({"tools"}), coding=True, frontier_worthy=True
)

PAID = "openrouter/aion-labs/aion-3.5-mini"
FREE_A = "openrouter/poolside/laguna-xs-2.1:free"
FREE_B = "openrouter/cohere/north-mini-code:free"
BARE_A = FREE_A.split("/", 1)[1]
BARE_B = FREE_B.split("/", 1)[1]


def _conn(provider: str = "openrouter", plan_label: str = "payg") -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": "apikey",
        "isActive": True,
        "testStatus": "ok",
        "plan_label": plan_label,
        "backoffLevel": 0,
        "import_free_only": True,
    }


def _row(route_id: str, owned_by: str = "openrouter") -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by,
        "capabilities": {"tool_calling": True, "chat": True},
        "context_length": 128000,
    }


def _free_set() -> OpenRouterFreeSet:
    entries = parse_free_entries(
        {
            "data": [
                {
                    "id": BARE_A,
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"output_modalities": ["text"]},
                },
                {
                    "id": BARE_B,
                    "pricing": {"prompt": "0", "completion": "0"},
                    "architecture": {"output_modalities": ["text"]},
                },
            ]
        }
    )
    return OpenRouterFreeSet(
        free_ids=frozenset(entries),
        entries=entries,
        fetched_at=NOW,
        source="live",
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )


def _agentic_cache(tmp_path: Path, *routes: str) -> HealthCache:
    """Fresh agentic tool proof so FREE routes pass the BOD-299 AC2 gate.

    REQ is frontier_worthy=True with tools, so FREE routes need agentic proof
    before selection reaches the pool-override recording these tests assert.
    """
    cache = HealthCache(tmp_path / "health-cache.json")
    for route in routes:
        cache.record(
            route,
            ProbeResult(
                category=CATEGORY_OK,
                chat_ok=True,
                tool_ok=True,
                probe_class="agentic",
                agentic_ok=True,
            ),
            NOW,
        )
    return cache


def _probe_with(paid_403: bool = True) -> dict[str, HealthResult]:
    results: dict[str, HealthResult] = {
        FREE_A: HealthResult(healthy=False, category="timeout", status_code=None),
        FREE_B: HealthResult(healthy=False, category="timeout", status_code=None),
    }
    if paid_403:
        results[PAID] = HealthResult(
            healthy=False,
            category="permission",
            status_code=403,
            error_message=f"[{PAID}] [403]: Key limit exceeded (total limit).",
        )
    return results


# ---------------------------------------------------------------------------
# G1
# ---------------------------------------------------------------------------


class TestG1PoolNotCooledByPaidKeyLimit:
    def test_paid_openrouter_key_limit_403_does_not_cool_pool_or_provider(
        self, tmp_path: Path
    ) -> None:
        results = _probe_with(paid_403=True)
        probe_calls: list[str] = []

        def probe(route_id: str) -> HealthResult:
            probe_calls.append(route_id)
            return results[route_id]

        ladder = EligibilityLadder(
            [_row(PAID), _row(FREE_A), _row(FREE_B)],
            [_conn()],
            probe,
            tmp_path / "state.json",
            openrouter_free_set=_free_set(),
        )
        _chosen, verdicts = ladder.select(REQ, now=NOW)

        state = json.loads((tmp_path / "state.json").read_text())
        cds = state.get("cooldowns", {})
        assert "pool:openrouter" not in cds, cds.keys()
        assert "provider:openrouter" not in cds, cds.keys()
        for v in verdicts:
            if v.route_id in (FREE_A, FREE_B):
                assert v.reason != "cooldown:provider", (v.route_id, v.reason)

    def test_paid_key_limit_403_writes_no_pool_not_free_override(self, tmp_path: Path) -> None:
        """BOD-418 review: a paid OpenRouter key-limit 403 must not write a
        ``pool:`` not-free override that would exclude free siblings."""
        results = _probe_with(paid_403=True)

        def probe(route_id: str) -> HealthResult:
            return results[route_id]

        ladder = EligibilityLadder(
            [_row(PAID), _row(FREE_A), _row(FREE_B)],
            [_conn()],
            probe,
            tmp_path / "state.json",
            openrouter_free_set=_free_set(),
        )
        ladder.select(REQ, now=NOW)

        state = json.loads((tmp_path / "state.json").read_text())
        overrides = state.get("not_free_overrides", {})
        # Paid key-limit 403 is permission category — not a not-free signal —
        # so no override is written at all. The route-scoped cooldown is the
        # only record, and it must not touch the free pool.
        assert "pool:openrouter-free" not in overrides, overrides.keys()
        assert "pool:openrouter" not in overrides, overrides.keys()
        # Free siblings remain selectable: no pool/provider cooldown and no
        # pool-level not-free override.
        for v in ladder.select(REQ, now=NOW)[1]:
            if v.route_id in (FREE_A, FREE_B):
                assert v.reason != "cooldown:provider", (v.route_id, v.reason)

    def test_payment_required_paid_free_suffix_route_writes_route_only_override(
        self, tmp_path: Path
    ) -> None:
        """A paid OpenRouter route whose id ends in ``:free`` maps to the shared
        ``openrouter-free`` pool via ``backend_pool``. A real not-free signal on
        it must write only the route override — never ``pool:openrouter-free`` —
        so genuine free siblings are not excluded."""
        paid_free_suffix = "openrouter/some-paid-model:free"
        results = {
            paid_free_suffix: HealthResult(
                healthy=False,
                category="payment_required",
                status_code=402,
                error_message=f"[{paid_free_suffix}] [402]: insufficient credits",
            ),
            FREE_A: HealthResult(healthy=False, category="timeout", status_code=None),
            FREE_B: HealthResult(healthy=False, category="timeout", status_code=None),
        }

        def probe(route_id: str) -> HealthResult:
            return results[route_id]

        ladder = EligibilityLadder(
            [_row(paid_free_suffix), _row(FREE_A), _row(FREE_B)],
            [_conn()],
            probe,
            tmp_path / "state.json",
            openrouter_free_set=_free_set(),
        )
        ladder.select(REQ, now=NOW)

        state = json.loads((tmp_path / "state.json").read_text())
        overrides = state.get("not_free_overrides", {})
        assert f"route:{paid_free_suffix}" in overrides, overrides.keys()
        assert "pool:openrouter-free" not in overrides, overrides.keys()

    def test_confirmed_free_payment_required_keeps_pool_override(self, tmp_path: Path) -> None:
        """Account-level fail-closed: a real not-free signal (payment_required)
        on a route inside the public 0/0 set still records route+pool override."""
        results = {
            FREE_A: HealthResult(
                healthy=False,
                category="payment_required",
                status_code=402,
                error_message=f"[{FREE_A}] [402]: insufficient credits",
            ),
            FREE_B: HealthResult(healthy=False, category="timeout", status_code=None),
        }

        def probe(route_id: str) -> HealthResult:
            return results[route_id]

        ladder = EligibilityLadder(
            [_row(FREE_A), _row(FREE_B)],
            [_conn()],
            probe,
            tmp_path / "state.json",
            openrouter_free_set=_free_set(),
            health_cache=_agentic_cache(tmp_path, FREE_A, FREE_B),
        )
        ladder.select(REQ, now=NOW)

        state = json.loads((tmp_path / "state.json").read_text())
        overrides = state.get("not_free_overrides", {})
        assert f"route:{FREE_A}" in overrides, overrides.keys()
        assert "pool:openrouter-free" in overrides, overrides.keys()


# ---------------------------------------------------------------------------
# G2: free-set injection into evidence classifiers
# ---------------------------------------------------------------------------


class TestG2FreeSetInjection:
    def test_omniroute_evidence_classifies_public_free_as_free(self) -> None:
        from verdict.orchestration.eligibility import _classify_capacity

        conn = _conn()
        row = {"id": FREE_A, "owned_by": "openrouter"}
        cls, _, evidence = _classify_capacity(
            conn, row, route_id=FREE_A, openrouter_free_set=_free_set()
        )
        assert cls is CapacityClass.FREE, (cls, evidence)

    def test_omniroute_evidence_classifies_paid_as_metered(self) -> None:
        from verdict.orchestration.eligibility import _classify_capacity

        conn = _conn()
        row = {"id": PAID, "owned_by": "openrouter"}
        cls, _, evidence = _classify_capacity(
            conn, row, route_id=PAID, openrouter_free_set=_free_set()
        )
        assert cls is CapacityClass.METERED, (cls, evidence)

    def test_verified_models_classifies_public_free_as_free(self) -> None:
        from verdict.orchestration.verified_models import _capacity_for_row

        conn = _conn()
        row = {"id": FREE_A, "owned_by": "openrouter"}
        cls, evidence = _capacity_for_row(row, conn, openrouter_free_set=_free_set())
        assert cls is CapacityClass.FREE, (cls, evidence)

    def test_verified_models_classifies_paid_as_metered(self) -> None:
        from verdict.orchestration.verified_models import _capacity_for_row

        conn = _conn()
        row = {"id": PAID, "owned_by": "openrouter"}
        cls, evidence = _capacity_for_row(row, conn)
        assert cls is CapacityClass.METERED, (cls, evidence)


# ---------------------------------------------------------------------------
# G3: autodev suffix trust
# ---------------------------------------------------------------------------


class TestG3AutodevNoSuffixTrust:
    def test_classify_route_capacity_metered_for_free_suffix_outside_public_set(self) -> None:
        from verdict.orchestration.eligibility import _classify_capacity

        conn = _conn()
        row = {"id": "openrouter/some-paid-model:free", "owned_by": "openrouter"}
        cls, _, evidence = _classify_capacity(
            conn, row, route_id="openrouter/some-paid-model:free", openrouter_free_set=_free_set()
        )
        assert cls is CapacityClass.METERED, (cls, evidence)


# ---------------------------------------------------------------------------
# G4: cache refresh in acceptance
# ---------------------------------------------------------------------------


class TestG4CacheRefresh:
    def test_acceptance_imports_load_openrouter_free_set(self) -> None:
        import verdict.acceptance as acceptance

        assert hasattr(acceptance, "load_openrouter_free_set"), (
            "acceptance.py must import load_openrouter_free_set to refresh the cache"
        )

    def test_acceptance_refresh_with_margin_prevents_cache_gap(self, tmp_path: Path) -> None:
        """End-to-end replay of the acceptance refresh cadence gap.

        Simulates:
        1. Acceptance tick T0 (refresh=True, min_remaining_seconds=7200) -> fetches live
        2. Acceptance tick T0+6h-1.3s (refresh=True, min_remaining_seconds=7200) -> fetches live (does not return cache)
        3. Cache-only reader (refresh=False) 30s later -> gets set (no gap)
        """
        from datetime import timedelta, timezone

        from verdict.openrouter_free_set import load_openrouter_free_set

        cache_path = tmp_path / "openrouter-free-set.json"
        fetch_calls: list[dict[str, Any]] = []

        def counting_fetch(url: str, timeout: float) -> dict[str, Any]:
            fetch_calls.append({"url": url, "timeout": timeout})
            return {
                "data": [
                    {
                        "id": "poolside/laguna-xs-2.1:free",
                        "pricing": {"prompt": "0", "completion": "0"},
                        "architecture": {"output_modalities": ["text"]},
                    }
                ]
            }

        t0 = datetime(2026, 11, 15, 0, 0, 16, 460000, tzinfo=timezone.utc)
        ttl = 21600.0

        # Step 1: First acceptance tick at T0 -> fetches live
        first = load_openrouter_free_set(
            now=t0,
            cache_path=cache_path,
            ttl_seconds=ttl,
            fetch=counting_fetch,
            refresh=True,
            min_remaining_seconds=7200.0,
        )
        assert first is not None
        assert first.source == "live"
        assert len(fetch_calls) == 1

        # Step 2: Second acceptance tick at T0 + 6h - 1.3s (21598.7s)
        # With margin 7200s: age (21598.7) + 7200 >= 21600 => refetches live
        t1 = t0 + timedelta(seconds=21598.7)
        second = load_openrouter_free_set(
            now=t1,
            cache_path=cache_path,
            ttl_seconds=ttl,
            fetch=counting_fetch,
            refresh=True,
            min_remaining_seconds=7200.0,
        )
        assert second is not None
        assert second.source == "live"
        assert len(fetch_calls) == 2

        # Step 3: Cache-only reader 30s later (refresh=False) -> gets the freshly written set
        t2 = t1 + timedelta(seconds=30)
        reader = load_openrouter_free_set(
            now=t2, cache_path=cache_path, ttl_seconds=ttl, refresh=False
        )
        assert reader is not None
        assert reader.source == "cache"
        assert reader.contains("poolside/laguna-xs-2.1:free")


# ---------------------------------------------------------------------------
# G5 / G6: key-limit scoping and stale drop
# ---------------------------------------------------------------------------


class TestG5KeyLimitScoping:
    def test_key_limit_on_non_free_is_route_scoped_true_for_paid(self) -> None:
        detail = f"[{PAID}] [403]: Key limit exceeded (total limit)."
        assert (
            key_limit_on_non_free_is_route_scoped(
                route_id=PAID, http_status=403, detail=detail, free_set=_free_set()
            )
            is True
        )

    def test_key_limit_on_free_is_account_level(self) -> None:
        detail = f"[{FREE_A}] [403]: Key limit exceeded (total limit)."
        assert (
            key_limit_on_non_free_is_route_scoped(
                route_id=FREE_A, http_status=403, detail=detail, free_set=_free_set()
            )
            is False
        )

    def test_openrouter_route_is_public_free_detects_member(self) -> None:
        assert openrouter_route_is_public_free(FREE_A, _free_set()) is True
        assert openrouter_route_is_public_free(PAID, _free_set()) is False


class TestG6StaleFreeDrop:
    def test_stale_free_404_is_route_scoped_not_pool_cooled(self) -> None:
        from verdict.orchestration.credential_pools import POOL_COOLDOWN_CATEGORIES

        assert CATEGORY_NOT_FOUND not in POOL_COOLDOWN_CATEGORIES
        detail = "[openrouter/apodex-1.1-mini:free] [404]: unavailable for free"
        assert (
            key_limit_on_non_free_is_route_scoped(
                route_id="openrouter/apodex-1.1-mini:free",
                http_status=404,
                detail=detail,
                free_set=_free_set(),
            )
            is False
        )
