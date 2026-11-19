"""Tests for verdict.discovery_funnel (plan 20261119-model-discovery S0).

All tests are fixture-based: no live calls, no network, no ~/.verdict writes.
Every test that invokes run_funnel() points VERDICT_HOME at a temp dir so the
real ~/.verdict is never read or written.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.discovery_funnel import (
    CAPS,
    FUNNEL_SCHEMA,
    _load_admission,
    _load_identify_report,
    build_funnel,
    render_plain,
    run_funnel,
)
from verdict.orchestration.health_cache import HealthCache, HealthEntry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=timezone.utc)


def _make_health_entry(
    *,
    route_id: str,
    http_status: int | None = 200,
    agentic_ok: bool = False,
    capacity_evidence: str = "plan_free",
    checked_at: datetime | None = None,
) -> HealthEntry:
    """Return a minimal HealthEntry suitable for funnel tests."""
    return HealthEntry(
        checked_at=checked_at or NOW,
        chat_ok=True,
        http_status=http_status,
        agentic_ok=agentic_ok,
        capacity_evidence=capacity_evidence,
    )


def _write_health_cache(path: Path, routes: dict[str, dict[str, Any]]) -> None:
    payload = {"routes": routes, "schema_version": "1"}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_identify_report(path: Path, *, inventory_route_count: int) -> None:
    report: dict[str, Any] = {
        "inventory_route_count": inventory_route_count,
        "mode": "passive",
        "task_pools": {},
        "source_errors": [],
    }
    path.write_text(json.dumps(report), encoding="utf-8")


def _write_admission(path: Path, *, admitted: list[str]) -> None:
    payload: dict[str, Any] = {
        "schema": "verdict.admission/v1",
        "admitted": admitted,
        "candidates": [
            {"admitted": True, "route_id": r, "reason": "admitted_unverified"} for r in admitted
        ],
        "generated_at": NOW.isoformat(),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit tests: build_funnel with injected fixtures
# ---------------------------------------------------------------------------


class TestBuildFunnelAllSources:
    """build_funnel with all evidence sources populated."""

    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        hc_path = Path(self.tmp) / "health-cache.json"
        # Route A: answered, tool_ok, admitted
        # Route B: answered only (no agentic proof)
        # Route C: probed but no HTTP response (timeout)
        routes: dict[str, Any] = {
            "prov/route-a": {
                "route_id": "prov/route-a",
                "category": "",
                "checked_at": NOW.isoformat(),
                "until": NOW.isoformat(),
                "consecutive_failures": 0,
                "chat_ok": True,
                "tool_ok": True,
                "http_status": 200,
                "agentic_ok": True,
                "agentic_checked_at": NOW.isoformat(),
                "capacity_evidence": "plan_free",
                "healthy": True,
            },
            "prov/route-b": {
                "route_id": "prov/route-b",
                "category": "",
                "checked_at": NOW.isoformat(),
                "until": NOW.isoformat(),
                "consecutive_failures": 0,
                "chat_ok": True,
                "tool_ok": False,
                "http_status": 200,
                "agentic_ok": False,
                "capacity_evidence": "provider_wide_free",
                "healthy": True,
            },
            "prov/route-c": {
                "route_id": "prov/route-c",
                "category": "",
                "checked_at": NOW.isoformat(),
                "until": NOW.isoformat(),
                "consecutive_failures": 1,
                "chat_ok": False,
                "tool_ok": False,
                "agentic_ok": False,
                "capacity_evidence": "plan_free",
                "healthy": False,
            },
        }
        _write_health_cache(hc_path, routes)

        self.identify_report = {
            "inventory_route_count": 100,
            "mode": "passive",
            "task_pools": {},
            "source_errors": [],
        }
        self.admission = {
            "schema": "verdict.admission/v1",
            "admitted": ["prov/route-a"],
            "candidates": [
                {"admitted": True, "route_id": "prov/route-a", "reason": "admitted_unverified"},
                {"admitted": False, "route_id": "prov/route-b", "reason": "health_negative"},
            ],
            "generated_at": NOW.isoformat(),
        }
        self.hc = HealthCache(hc_path)

    def test_listed_count(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        assert result["stages"]["listed"]["count"] == 100

    def test_probed_count(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        assert result["stages"]["probed"]["count"] == 3

    def test_answered_count(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        # route-a and route-b have http_status; route-c does not
        assert result["stages"]["answered"]["count"] == 2

    def test_tool_ok_count(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        # Only route-a has agentic_ok=True
        assert result["stages"]["tool_ok"]["count"] == 1

    def test_admitted_count(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        assert result["stages"]["admitted"]["count"] == 1

    def test_admitted_drops_named(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        drops = result["stages"]["admitted"]["drops"]
        assert "health_negative" in drops

    def test_schema(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        assert result["schema"] == FUNNEL_SCHEMA

    def test_caps_present(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        for cap_name in CAPS:
            assert cap_name in result["caps"]

    def test_considered_cap_name(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
            max_probes_per_select=8,
        )
        considered = result["stages"]["considered"]
        assert considered["cap"] == "max_probes_per_select"
        assert considered["cap_value"] == 8

    def test_selected_is_null(self) -> None:
        result = build_funnel(
            identify_report=self.identify_report,
            health_cache=self.hc,
            admission=self.admission,
            now=NOW,
        )
        assert result["stages"]["selected"]["count"] is None


class TestBuildFunnelAllMissing:
    """build_funnel with no evidence: every stage reports unknown (None)."""

    def test_all_stages_unknown(self) -> None:
        result = build_funnel(identify_report=None, health_cache=None, admission=None, now=NOW)
        for stage_name in ("listed", "probed", "answered", "tool_ok", "admitted"):
            count = result["stages"][stage_name]["count"]
            assert count is None, f"stage {stage_name!r} should be unknown, got {count}"

    def test_unknown_is_not_zero(self) -> None:
        """'Unknown' must never be reported as 0 (AGENTS.md invariant)."""
        result = build_funnel(identify_report=None, health_cache=None, admission=None, now=NOW)
        for stage_name in ("listed", "probed", "answered", "tool_ok", "admitted"):
            count = result["stages"][stage_name]["count"]
            assert count != 0, f"stage {stage_name!r} reported 0, should be None"


class TestBuildFunnelHealthCacheOnlyNoAdmission:
    """Health cache present but admission missing: stages through tool_ok have counts."""

    def setup_method(self) -> None:
        self.tmp = tempfile.mkdtemp()
        hc_path = Path(self.tmp) / "health-cache.json"
        routes = {
            "p/r1": {
                "route_id": "p/r1",
                "category": "",
                "checked_at": NOW.isoformat(),
                "until": NOW.isoformat(),
                "consecutive_failures": 0,
                "chat_ok": True,
                "tool_ok": True,
                "http_status": 200,
                "agentic_ok": True,
                "agentic_checked_at": NOW.isoformat(),
                "capacity_evidence": "plan_free",
                "healthy": True,
            }
        }
        _write_health_cache(hc_path, routes)
        self.hc = HealthCache(hc_path)

    def test_probed_has_count(self) -> None:
        result = build_funnel(identify_report=None, health_cache=self.hc, admission=None, now=NOW)
        assert result["stages"]["probed"]["count"] == 1

    def test_admitted_unknown(self) -> None:
        result = build_funnel(identify_report=None, health_cache=self.hc, admission=None, now=NOW)
        assert result["stages"]["admitted"]["count"] is None


# ---------------------------------------------------------------------------
# No-writes assertion
# ---------------------------------------------------------------------------


class TestNoWritesInVerdictHome:
    """run_funnel must not write any files to VERDICT_HOME."""

    def test_no_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
        monkeypatch.setenv("VERDICT_RECEIPTS_DB", ":memory:")

        # Snapshot mtimes before
        before: dict[str, float] = {
            str(p): p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()
        }

        run_funnel(verdict_home_path=tmp_path)

        # Snapshot mtimes after
        after: dict[str, float] = {
            str(p): p.stat().st_mtime for p in tmp_path.rglob("*") if p.is_file()
        }

        new_files = set(after.keys()) - set(before.keys())
        changed = {k for k, v in before.items() if after.get(k, v) != v}
        assert not new_files, f"run_funnel wrote new files: {new_files}"
        assert not changed, f"run_funnel modified files: {changed}"


# ---------------------------------------------------------------------------
# JSON schema validation
# ---------------------------------------------------------------------------


class TestJsonSchemaValidation:
    """--json output must validate against schemas/discovery-funnel.v1.json."""

    def test_output_validates(self, tmp_path: Path) -> None:
        jsonschema = pytest.importorskip("jsonschema")

        schema_path = Path(__file__).parent.parent / "schemas" / "discovery-funnel.v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        result = build_funnel(
            identify_report={"inventory_route_count": 10, "task_pools": {}},
            health_cache=None,
            admission=None,
            now=NOW,
        )
        # Should not raise:
        jsonschema.validate(result, schema)

    def test_output_schema_field(self) -> None:
        result = build_funnel(identify_report=None, health_cache=None, admission=None, now=NOW)
        assert result["schema"] == "verdict.discovery.funnel/v1"


# ---------------------------------------------------------------------------
# Plain-text renderer
# ---------------------------------------------------------------------------


class TestRenderPlain:
    """render_plain produces human-readable output."""

    def test_contains_stage_names(self) -> None:
        result = build_funnel(identify_report=None, health_cache=None, admission=None, now=NOW)
        text = render_plain(result)
        for stage in ("listed", "probed", "answered", "tool_ok", "admitted"):
            assert stage in text

    def test_unknown_shown_as_unknown(self) -> None:
        result = build_funnel(identify_report=None, health_cache=None, admission=None, now=NOW)
        text = render_plain(result)
        assert "unknown" in text

    def test_caps_shown(self) -> None:
        result = build_funnel(identify_report=None, health_cache=None, admission=None, now=NOW)
        text = render_plain(result)
        assert "SCREEN_MAX_ROUTES" in text


# ---------------------------------------------------------------------------
# Loader helpers
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_load_identify_missing(self, tmp_path: Path) -> None:
        assert _load_identify_report(tmp_path / "nope.json") is None

    def test_load_identify_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert _load_identify_report(p) is None

    def test_load_identify_non_object(self, tmp_path: Path) -> None:
        p = tmp_path / "list.json"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        assert _load_identify_report(p) is None

    def test_load_admission_missing(self, tmp_path: Path) -> None:
        assert _load_admission(tmp_path / "nope.json") is None

    def test_load_admission_valid(self, tmp_path: Path) -> None:
        p = tmp_path / "admission.json"
        _write_admission(p, admitted=["a/b"])
        result = _load_admission(p)
        assert result is not None
        assert "admitted" in result
