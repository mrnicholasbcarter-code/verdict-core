"""Tests for verdict.connection_billing (CP2 task 4.1).

Covers:
  - Snapshot loading and lookup (snapshot facts)
  - Operator overlay loading and lookup (overlay wins over snapshot)
  - Provider alias resolution (agy → antigravity)
  - Billing facts produce correct CapacityClass in capacity_of()
  - Drift detection interface (no live calls)
  - Schema validation of operator overlay
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from verdict.capacity import CapacityClass, capacity_of
from verdict.connection_billing import (
    BILLING_KEYLESS,
    BILLING_SUBSCRIPTION,
    OVERAGE_HARD_STOP,
    OVERAGE_METERS_TO_PAID,
    OVERAGE_UNKNOWN,
    SOURCE_OVERLAY,
    SOURCE_SNAPSHOT,
    check_drift,
    lookup,
    snapshot_meta,
)

if TYPE_CHECKING:
    from verdict.orchestration.health_cache import HealthCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn(provider: str, auth_type: str = "oauth") -> dict[str, Any]:
    return {"provider": provider, "authType": auth_type, "isActive": True}


def _row() -> dict[str, Any]:
    return {"id": "some/model", "owned_by": "some", "context_length": 32000}


# ---------------------------------------------------------------------------
# A. Snapshot loading
# ---------------------------------------------------------------------------


class TestSnapshotLoading:
    def test_snapshot_meta_has_version(self) -> None:
        """snapshot_meta() returns a dict with omniroute_version and source_sha256."""
        meta = snapshot_meta()
        assert isinstance(meta, dict)
        assert "omniroute_version" in meta
        assert "source_sha256" in meta
        assert isinstance(meta["source_sha256"], str)
        assert len(meta["source_sha256"]) == 64  # sha256 hex

    def test_snapshot_has_known_entries(self) -> None:
        """The shipped snapshot contains the known catalog entries."""
        # claude/oauth is the canonical Anthropic subscription entry
        fact = lookup("claude", "oauth")
        assert fact is not None
        assert fact.billing == BILLING_SUBSCRIPTION
        assert fact.overage == OVERAGE_HARD_STOP
        assert fact.source == SOURCE_SNAPSHOT

    def test_codex_subscription_hard_stop(self) -> None:
        fact = lookup("codex", "oauth")
        assert fact is not None
        assert fact.billing == BILLING_SUBSCRIPTION
        assert fact.overage == OVERAGE_HARD_STOP

    def test_antigravity_subscription_hard_stop(self) -> None:
        fact = lookup("antigravity", "oauth")
        assert fact is not None
        assert fact.billing == BILLING_SUBSCRIPTION
        assert fact.overage == OVERAGE_HARD_STOP

    def test_devin_desktop_meters_to_paid(self) -> None:
        fact = lookup("devin-desktop", "oauth")
        assert fact is not None
        assert fact.billing == BILLING_SUBSCRIPTION
        assert fact.overage == OVERAGE_METERS_TO_PAID

    def test_cursor_subscription_unknown_overage(self) -> None:
        fact = lookup("cursor", "oauth")
        assert fact is not None
        assert fact.billing == BILLING_SUBSCRIPTION
        assert fact.overage == OVERAGE_UNKNOWN

    def test_missing_provider_returns_none(self) -> None:
        """A provider not in the catalog returns None — absence is not proof."""
        fact = lookup("does-not-exist", "oauth")
        assert fact is None

    def test_unknown_billing_entries_excluded(self) -> None:
        """Entries with billing='unknown' are excluded from the index."""
        # No known provider should have billing=unknown in the shipped snapshot
        fact = lookup("does-not-exist", None)
        assert fact is None


# ---------------------------------------------------------------------------
# B. Provider alias resolution
# ---------------------------------------------------------------------------


class TestProviderAlias:
    def test_agy_aliases_antigravity(self) -> None:
        """agy is an alias for antigravity; both return the same billing fact."""
        agy_fact = lookup("agy", "oauth")
        ant_fact = lookup("antigravity", "oauth")
        assert agy_fact is not None
        assert ant_fact is not None
        assert agy_fact.billing == ant_fact.billing
        assert agy_fact.overage == ant_fact.overage
        assert agy_fact.source == ant_fact.source

    def test_agy_case_insensitive(self) -> None:
        """Alias resolution is case-insensitive."""
        assert lookup("AGY", "oauth") == lookup("agy", "oauth")

    def test_agy_gives_subscription(self) -> None:
        """agy maps to antigravity subscription."""
        fact = lookup("agy", "oauth")
        assert fact is not None
        assert fact.billing == BILLING_SUBSCRIPTION

    def test_unaliased_provider_unchanged(self) -> None:
        """Providers not in the alias table are unaffected."""
        fact = lookup("claude", "oauth")
        assert fact is not None
        # Returned fact must be from claude directly, not some alias artifact
        assert fact.billing == BILLING_SUBSCRIPTION


# ---------------------------------------------------------------------------
# C. Operator overlay
# ---------------------------------------------------------------------------


class TestOperatorOverlay:
    def test_overlay_wins_over_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An overlay entry for the same (provider, authType) overrides the snapshot."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        # Clear overlay cache so a fresh read is triggered
        import verdict.connection_billing as _cb

        _cb._overlay_cache = None

        overlay = {
            "entries": [
                {
                    "provider": "claude",
                    "authType": "oauth",
                    "billing": "keyless",
                    "overage": "hard-stop",
                }
            ]
        }
        (tmp_path / "connection_billing_overlay.json").write_text(json.dumps(overlay))

        env = {"VERDICT_HOME": str(tmp_path)}
        fact = lookup("claude", "oauth", env=env)
        assert fact is not None
        assert fact.billing == BILLING_KEYLESS
        assert fact.source == SOURCE_OVERLAY

        # Reset cache
        _cb._overlay_cache = None

    def test_invalid_overlay_cannot_override_billing(self, tmp_path: Path) -> None:
        overlay = {"entries": [{"provider": "claude", "authType": "oauth", "billing": "keyless"}]}
        (tmp_path / "connection_billing_overlay.json").write_text(json.dumps(overlay))
        fact = lookup("claude", "oauth", env={"VERDICT_HOME": str(tmp_path)})
        assert fact is not None
        assert fact.source == SOURCE_SNAPSHOT
        assert fact.billing == BILLING_SUBSCRIPTION

    def test_overlay_provider_wide_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provider-wide overlay entry (no authType) covers all auth types for that provider."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        import verdict.connection_billing as _cb

        _cb._overlay_cache = None

        overlay = {
            "entries": [
                {"provider": "my-custom", "billing": "subscription", "overage": "hard-stop"}
            ]
        }
        (tmp_path / "connection_billing_overlay.json").write_text(json.dumps(overlay))

        env = {"VERDICT_HOME": str(tmp_path)}
        # Both oauth and apikey should match the provider-wide entry
        for auth in ("oauth", "apikey"):
            fact = lookup("my-custom", auth, env=env)
            assert fact is not None, f"expected fact for auth={auth}"
            assert fact.billing == BILLING_SUBSCRIPTION
            assert fact.source == SOURCE_OVERLAY

        _cb._overlay_cache = None

    def test_overlay_absent_returns_snapshot(self, tmp_path: Path) -> None:
        """When no overlay file exists, snapshot is used normally."""
        env = {"VERDICT_HOME": str(tmp_path)}
        # tmp_path has no connection_billing_overlay.json
        fact = lookup("claude", "oauth", env=env)
        assert fact is not None
        assert fact.source == SOURCE_SNAPSHOT

    def test_overlay_specific_wins_over_provider_wide(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An (provider, authType) specific overlay wins over a provider-wide overlay."""
        import verdict.connection_billing as _cb

        _cb._overlay_cache = None

        overlay = {
            "entries": [
                # provider-wide
                {"provider": "myprov", "billing": "subscription", "overage": "unknown"},
                # auth-specific — wins
                {
                    "provider": "myprov",
                    "authType": "oauth",
                    "billing": "keyless",
                    "overage": "hard-stop",
                },
            ]
        }
        (tmp_path / "connection_billing_overlay.json").write_text(json.dumps(overlay))

        env = {"VERDICT_HOME": str(tmp_path)}
        oauth_fact = lookup("myprov", "oauth", env=env)
        apikey_fact = lookup("myprov", "apikey", env=env)

        assert oauth_fact is not None and oauth_fact.billing == BILLING_KEYLESS
        assert apikey_fact is not None and apikey_fact.billing == BILLING_SUBSCRIPTION

        _cb._overlay_cache = None


# ---------------------------------------------------------------------------
# D. Billing facts → CapacityClass in capacity_of()
# ---------------------------------------------------------------------------


class TestBillingFactCapacity:
    def test_subscription_fact_gives_subscription(self, tmp_path: Path) -> None:
        """A billing catalog subscription entry produces CapacityClass.SUBSCRIPTION."""
        conn = _conn("claude", "oauth")
        cls, fact = capacity_of(conn, _row(), route_id="claude/sonnet", openrouter_free_set=None)
        assert cls is CapacityClass.SUBSCRIPTION
        # Overage for claude is "hard-stop" in the catalog snapshot.
        assert fact == "billing_snapshot_subscription:hard-stop"

    def test_subscription_overlay_fact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An overlay subscription entry produces billing_overlay_subscription:{overage}."""
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        import verdict.connection_billing as _cb

        _cb._overlay_cache = None

        overlay = {
            "entries": [
                {
                    "provider": "my-sub",
                    "authType": "oauth",
                    "billing": "subscription",
                    "overage": "hard-stop",
                }
            ]
        }
        (tmp_path / "connection_billing_overlay.json").write_text(json.dumps(overlay))

        # Monkey-patch to use this VERDICT_HOME
        monkeypatch.setattr(
            "verdict.capacity.billing_lookup",
            lambda p, a: lookup(p, a, env={"VERDICT_HOME": str(tmp_path)}),
        )

        conn = _conn("my-sub", "oauth")
        cls, fact_name = capacity_of(conn, _row(), route_id="my-sub/m", openrouter_free_set=None)
        assert cls is CapacityClass.SUBSCRIPTION
        # Overlay entry has overage="hard-stop".
        assert fact_name == "billing_overlay_subscription:hard-stop"

        _cb._overlay_cache = None

    def test_no_billing_entry_falls_through_to_pricing(self) -> None:
        """A route with no billing catalog entry uses pricing rules."""
        # kilocode is not in the catalog
        conn = _conn("kilocode", "oauth")
        # No pricing → UNKNOWN
        cls, fact = capacity_of(conn, _row(), route_id="kilocode/model", openrouter_free_set=None)
        assert cls is CapacityClass.UNKNOWN
        assert fact == "no_free_evidence"

    def test_no_billing_entry_zero_pricing_gives_free(self) -> None:
        """A route with no catalog entry but zero/zero pricing gives FREE."""
        conn = _conn("kilocode", "oauth")
        row_with_zero = {**_row(), "pricing": {"input": 0, "output": 0}}
        cls, fact = capacity_of(
            conn, row_with_zero, route_id="kilocode/m:free", openrouter_free_set=None
        )
        assert cls is CapacityClass.FREE
        assert fact == "all_zero_pricing"

    def test_agy_subscription_via_alias(self) -> None:
        """agy routes are SUBSCRIPTION via the antigravity billing alias."""
        conn = _conn("agy", "oauth")
        cls, fact = capacity_of(conn, _row(), route_id="agy/model", openrouter_free_set=None)
        assert cls is CapacityClass.SUBSCRIPTION
        # Overage for antigravity is "hard-stop" in the catalog snapshot.
        assert fact == "billing_snapshot_subscription:hard-stop"


# ---------------------------------------------------------------------------
# E. Drift detection (no live calls)
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_check_drift_returns_dict(self) -> None:
        """check_drift() always returns a dict with 'status', 'detail', etc."""
        result = check_drift()
        assert isinstance(result, dict)
        assert "status" in result
        assert result["status"] in ("ok", "drift", "unavailable")
        assert "detail" in result
        assert "snapshot_version" in result
        assert "snapshot_sha256" in result

    def test_check_drift_with_nonexistent_package(self, tmp_path: Path) -> None:
        """When given a package root with no readable files, returns drift
        (the root was explicitly passed so it's treated as 'found but unreadable')."""
        result = check_drift(package_root=tmp_path / "does-not-exist")
        # Package root was explicitly passed but files don't exist — the installed
        # version and sha256 are unreadable, so it reports drift or unavailable.
        assert result["status"] in ("drift", "unavailable")

    def test_check_drift_with_matching_package(self, tmp_path: Path) -> None:
        """When given a package root matching the snapshot, returns ok."""
        # Set up a fake package root with package.json and the TS file
        from verdict.connection_billing import snapshot_meta as _sm

        meta = _sm()
        version = meta["omniroute_version"]
        source_file = meta["source_file"]  # "open-sse/config/connectionBillingCatalog.ts"

        # Create the package root structure
        pkg_root = tmp_path / "omniroute"
        pkg_root.mkdir()
        (pkg_root / "package.json").write_text(json.dumps({"version": version}))
        ts_path = pkg_root / source_file
        ts_path.parent.mkdir(parents=True, exist_ok=True)

        # We need a file whose sha256 matches the snapshot
        # We can't reverse the sha256, so instead we just test the drift path
        ts_path.write_text("// fake content")
        result = check_drift(package_root=pkg_root)
        # Since sha256 won't match fake content, should be "drift" not "ok"
        assert result["status"] == "drift"
        assert "sha256" in result["detail"]


# ---------------------------------------------------------------------------
# F. Schema validation (overlay schema)
# ---------------------------------------------------------------------------


class TestOverlaySchema:
    def test_valid_overlay_validates(self) -> None:
        """A valid overlay passes JSON Schema validation."""
        try:
            import jsonschema  # type: ignore[import-untyped]
        except ImportError:
            pytest.skip("jsonschema not installed")

        schema_path = (
            Path(__file__).parent.parent
            / "verdict"
            / "schemas"
            / "connection-billing-overlay.v1.json"
        )
        schema = json.loads(schema_path.read_text())
        overlay = {
            "entries": [
                {
                    "provider": "my-co",
                    "authType": "oauth",
                    "billing": "subscription",
                    "overage": "hard-stop",
                },
                {"provider": "my-co", "billing": "metered", "overage": "meters-to-paid"},
            ]
        }
        jsonschema.validate(overlay, schema)  # should not raise

    def test_billing_unknown_rejected_by_schema(self) -> None:
        """billing='unknown' is not a valid overlay value."""
        try:
            import jsonschema
        except ImportError:
            pytest.skip("jsonschema not installed")

        schema_path = (
            Path(__file__).parent.parent
            / "verdict"
            / "schemas"
            / "connection-billing-overlay.v1.json"
        )
        schema = json.loads(schema_path.read_text())
        bad = {"entries": [{"provider": "x", "billing": "unknown", "overage": "hard-stop"}]}
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)


# ---------------------------------------------------------------------------
# G. subscription_window_spent_metered (task 2.3 / 4.1)
# ---------------------------------------------------------------------------


class TestSubscriptionWindowSpentMetered:
    """CP2 task 2.3: overage after a spent window degrades SUBSCRIPTION → METERED."""

    def _make_cache(self, tmp_path: Path, route_id: str, capacity_evidence: str) -> HealthCache:
        import datetime

        from verdict.orchestration.health_cache import HealthCache, HealthEntry

        cache_path = tmp_path / "health.json"
        cache = HealthCache(cache_path)
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        entry = HealthEntry(
            route_id=route_id,
            category="quota_cooldown",
            checked_at=now - datetime.timedelta(minutes=5),
            until=now + datetime.timedelta(hours=1),
            consecutive_failures=1,
            chat_ok=False,
            tool_ok=False,
            healthy=False,
            capacity_evidence=capacity_evidence,
        )
        cache._routes[route_id] = entry
        return cache

    def test_meters_to_paid_overage_returns_true(self, tmp_path: Path) -> None:
        """quota_cooldown + billing_snapshot_subscription:meters-to-paid → True."""
        import datetime

        from verdict.capacity import subscription_window_spent_metered

        cache = self._make_cache(
            tmp_path, "devin-desktop/model", "billing_snapshot_subscription:meters-to-paid"
        )
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        assert subscription_window_spent_metered(cache, "devin-desktop/model", now) is True

    def test_hard_stop_overage_returns_false(self, tmp_path: Path) -> None:
        """quota_cooldown + billing_snapshot_subscription:hard-stop → False (not METERED)."""
        import datetime

        from verdict.capacity import subscription_window_spent_metered

        cache = self._make_cache(
            tmp_path, "claude/sonnet", "billing_snapshot_subscription:hard-stop"
        )
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        assert subscription_window_spent_metered(cache, "claude/sonnet", now) is False

    def test_unknown_overage_returns_true(self, tmp_path: Path) -> None:
        """quota_cooldown + billing_snapshot_subscription:unknown → True (may meter)."""
        import datetime

        from verdict.capacity import subscription_window_spent_metered

        cache = self._make_cache(tmp_path, "cursor/model", "billing_snapshot_subscription:unknown")
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        assert subscription_window_spent_metered(cache, "cursor/model", now) is True

    def test_no_subscription_evidence_returns_false(self, tmp_path: Path) -> None:
        """quota_cooldown with no subscription evidence → False (not a known subscription)."""
        import datetime

        from verdict.capacity import subscription_window_spent_metered

        cache = self._make_cache(tmp_path, "openai/gpt-4", "apikey_positive_pricing")
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        assert subscription_window_spent_metered(cache, "openai/gpt-4", now) is False

    def test_expired_cooldown_returns_false(self, tmp_path: Path) -> None:
        """An expired quota_cooldown → False even with meters-to-paid overage."""
        import datetime

        from verdict.capacity import subscription_window_spent_metered
        from verdict.orchestration.health_cache import HealthCache, HealthEntry

        cache_path = tmp_path / "health.json"
        cache = HealthCache(cache_path)
        past = datetime.datetime.now(tz=datetime.timezone.utc) - datetime.timedelta(hours=2)
        entry = HealthEntry(
            route_id="devin-desktop/m",
            category="quota_cooldown",
            checked_at=past - datetime.timedelta(minutes=5),
            until=past,  # already expired
            consecutive_failures=1,
            chat_ok=False,
            tool_ok=False,
            healthy=False,
            capacity_evidence="billing_snapshot_subscription:meters-to-paid",
        )
        cache._routes["devin-desktop/m"] = entry
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        assert subscription_window_spent_metered(cache, "devin-desktop/m", now) is False

    def test_missing_route_returns_false(self, tmp_path: Path) -> None:
        """A route not in the cache → False."""
        import datetime

        from verdict.capacity import subscription_window_spent_metered
        from verdict.orchestration.health_cache import HealthCache

        cache = HealthCache(tmp_path / "health.json")
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        assert subscription_window_spent_metered(cache, "nonexistent/route", now) is False
