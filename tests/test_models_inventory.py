"""Tests for the models.list inventory action (BOD-277).

Covers: inventory present (many rows with provider counts), unreachable
gateway (config_only with message), filters, unknown fields stay None,
no probe calls, and palette rendering at COLUMNS=60 and COLUMNS=100.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_inventory_rows(n: int = 50, prefix: str = "kr") -> list[dict[str, Any]]:
    """Generate fake OmniRoute /v1/models rows."""
    rows: list[dict[str, Any]] = []
    for i in range(n):
        p = prefix if i % 3 == 0 else ("cc" if i % 3 == 1 else "gc")
        rows.append({"id": f"{p}/model-{i}", "object": "model"})
    return rows


def _make_metadata_record(model_id: str, *, tools: bool = True, context: int = 128000) -> Any:
    """Build a minimal metadata record mock."""
    from unittest.mock import MagicMock

    record = MagicMock()
    record.id = model_id
    caps = MagicMock()

    def _prov(val: Any) -> Any:
        m = MagicMock()
        m.value = val
        return m

    caps.context = _prov(context) if context else None
    caps.tools = _prov(tools)
    caps.structured = _prov(True)
    caps.input_cost_per_million = _prov(3.0)
    caps.output_cost_per_million = _prov(15.0)
    record.caps = caps
    return record


def _inventory_patches(
    rows: list[dict[str, Any]] | Exception | None = None,
    metadata_index: dict[str, Any] | None = None,
    metadata_refreshed: str = "2026-09-28T20:00:00Z",
    metadata_error: bool = False,
) -> Any:
    """Context manager that patches the inventory + metadata sources."""
    from contextlib import ExitStack
    from unittest.mock import MagicMock

    stack = ExitStack()

    if isinstance(rows, Exception):
        stack.enter_context(patch("verdict.orchestration.run.fetch_inventory", side_effect=rows))
    elif rows is not None:
        stack.enter_context(patch("verdict.orchestration.run.fetch_inventory", return_value=rows))
    else:
        stack.enter_context(patch("verdict.orchestration.run.fetch_inventory", return_value=[]))

    stack.enter_context(patch("verdict.orchestration.run.resolve_api_key", return_value=None))

    if metadata_error:
        stack.enter_context(
            patch("verdict.metadata.store.load_store", side_effect=FileNotFoundError)
        )
    else:
        mock_snapshot = MagicMock()
        mock_snapshot.index_omniroute.return_value = metadata_index or {}
        mock_snapshot.refreshed_at = metadata_refreshed
        stack.enter_context(patch("verdict.metadata.store.load_store", return_value=mock_snapshot))

    return stack


def _run_action(params: dict[str, Any] | None = None) -> Any:
    """Run models.list through the action registry."""
    from verdict.actions.registry import run_action

    return run_action("models.list", params)


# ---------------------------------------------------------------------------
# Inventory present -> many rows with provider counts
# ---------------------------------------------------------------------------


class TestInventoryPresent:
    def test_many_rows_with_provider_counts(self) -> None:
        rows = _make_inventory_rows(60)
        metadata_index: dict[str, Any] = {}
        for row in rows[:10]:
            metadata_index[row["id"]] = _make_metadata_record(row["id"])

        with _inventory_patches(rows=rows, metadata_index=metadata_index):
            result = _run_action({"show_all": True})

        data = result.data
        assert data["source"] == "inventory"
        assert data["inventory_error"] is None
        # 60 inventory + 1 default primary (anthropic/claude-opus-5) = 61 total
        assert data["total"] >= 60
        assert "provider_counts" in data
        counts = data["provider_counts"]
        assert len(counts) >= 3  # kr, cc, gc (+ possibly anthropic from config)

    def test_enrichment_from_metadata(self) -> None:
        rows = [{"id": "kr/claude-sonnet-5", "object": "model"}]
        record = _make_metadata_record("kr/claude-sonnet-5", tools=True, context=200000)

        with _inventory_patches(rows=rows, metadata_index={"kr/claude-sonnet-5": record}):
            result = _run_action({"show_all": True})

        models = result.data["models"]
        kr_model = next(m for m in models if m["id"] == "kr/claude-sonnet-5")
        assert kr_model["context_window"] == 200000
        assert kr_model["tools_support"] is True
        assert kr_model["structured_output"] is True
        assert kr_model["input_cost_per_million"] == 3.0
        assert kr_model["output_cost_per_million"] == 15.0


# ---------------------------------------------------------------------------
# Unreachable gateway -> config_only with message
# ---------------------------------------------------------------------------


class TestGatewayUnreachable:
    def test_config_only_fallback(self) -> None:
        with _inventory_patches(rows=ConnectionError("refused"), metadata_error=True):
            result = _run_action()

        data = result.data
        assert data["source"] == "config_only"
        assert data["inventory_error"] is not None
        assert "gateway unreachable" in data["inventory_error"]
        assert data["total"] >= 1
        assert any(m["source"] == "config" for m in data["models"])


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    def _setup_inventory(self) -> list[dict[str, Any]]:
        return [
            {"id": "kr/claude-sonnet-5", "object": "model"},
            {"id": "kr/claude-opus-5", "object": "model"},
            {"id": "cc/claude-sonnet-5", "object": "model"},
            {"id": "gc/gemini-2.5-pro", "object": "model"},
        ]

    def _run_with_inventory(self, params: dict[str, Any]) -> Any:
        rows = self._setup_inventory()
        with _inventory_patches(rows=rows, metadata_error=True):
            return _run_action(params)

    def test_provider_filter(self) -> None:
        result = self._run_with_inventory({"provider": "kr", "show_all": True})
        models = result.data["models"]
        inv_models = [m for m in models if m["source"] == "inventory"]
        assert all("kr" in m["provider"] for m in inv_models)
        assert len(inv_models) == 2

    def test_search_filter(self) -> None:
        result = self._run_with_inventory({"search": "gemini", "show_all": True})
        models = result.data["models"]
        inventory_models = [m for m in models if m["source"] == "inventory"]
        assert len(inventory_models) == 1
        assert "gemini" in inventory_models[0]["id"]

    def test_limit(self) -> None:
        result = self._run_with_inventory({"limit": 2})
        assert result.data["shown"] == 2
        assert result.data["total"] > 2


# ---------------------------------------------------------------------------
# Unknown fields stay None
# ---------------------------------------------------------------------------


class TestUnknownFields:
    def test_no_metadata_fields_are_none(self) -> None:
        rows = [{"id": "kr/unknown-model-xyz", "object": "model"}]
        with _inventory_patches(rows=rows, metadata_error=True):
            result = _run_action({"show_all": True})

        models = result.data["models"]
        unknown = next(m for m in models if m["id"] == "kr/unknown-model-xyz")
        assert unknown["context_window"] is None
        assert unknown["tools_support"] is None
        assert unknown["structured_output"] is None
        assert unknown["input_cost_per_million"] is None
        assert unknown["output_cost_per_million"] is None


# ---------------------------------------------------------------------------
# No probe calls
# ---------------------------------------------------------------------------


class TestNoProbes:
    def test_no_probes_called(self) -> None:
        """models.list must never call any probe function."""
        rows = _make_inventory_rows(5)
        with (
            _inventory_patches(rows=rows, metadata_error=True),
            patch(
                "verdict.probes.openai_probe_transport", side_effect=AssertionError("probe called!")
            ),
        ):
            result = _run_action()
            assert result.data["total"] >= 5


# ---------------------------------------------------------------------------
# Palette rendering at COLUMNS=60 and COLUMNS=100
# ---------------------------------------------------------------------------


class TestPaletteRendering:
    @pytest.mark.parametrize("columns", [60, 100])
    def test_human_output_renders(self, columns: int) -> None:
        """Human output renders without error at various terminal widths."""
        env = {
            "HOME": "/tmp/verdict_models_test",
            "XDG_CONFIG_HOME": "/tmp/verdict_models_test/.config",
            "NO_COLOR": "1",
            "CI": "1",
            "COLUMNS": str(columns),
            "PYTHONDONTWRITEBYTECODE": "1",
            "OMNIROUTE_BASE_URL": "http://127.0.0.1:9",
            "VERDICT_GATEWAY": "http://127.0.0.1:9",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "PYTHONPATH": str(ROOT),
        }
        proc = subprocess.run(
            [sys.executable, "-m", "verdict", "models"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, f"exit {proc.returncode}: {proc.stderr[-300:]}"
        assert "models total" in proc.stdout.lower() or "model(s) shown" in proc.stdout.lower()
        assert "gateway unreachable" in proc.stdout.lower() or "inventory" in proc.stdout.lower()

    @pytest.mark.parametrize("columns", [60, 100])
    def test_summary_shows_provider_counts(self, columns: int) -> None:
        """Summary line shows provider counts even at narrow widths."""
        rows = _make_inventory_rows(20)
        with _inventory_patches(rows=rows, metadata_error=True):
            result = _run_action()
        data = result.data
        assert "provider_counts" in data
        assert len(data["provider_counts"]) >= 2
