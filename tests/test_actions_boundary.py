"""Tests for BOD-275 Lane A: dependency direction boundary enforcement.

Checks that:
1. verdict/actions/**/*.py never imports verdict.cli or verdict.commands
2. cli.py aliases are identical objects to the canonical functions
3. doctor --json output is unchanged
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


class TestActionsBoundary:
    """AST scan: verdict/actions/ must never import verdict.cli or verdict.commands."""

    @staticmethod
    def _collect_action_files() -> list[Path]:
        """Collect all Python files under verdict/actions/."""
        actions_dir = ROOT / "verdict" / "actions"
        return sorted(actions_dir.rglob("*.py"))

    @staticmethod
    def _extract_imports(filepath: Path) -> list[tuple[int, str]]:
        """Return (line_no, import_target) for every import in the file."""
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
        results: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    results.append((node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                results.append((node.lineno, module))
        return results

    def test_no_cli_import_in_actions(self) -> None:
        """No file under verdict/actions/ imports verdict.cli or verdict.commands."""
        violations: list[str] = []
        for filepath in self._collect_action_files():
            for lineno, target in self._extract_imports(filepath):
                if target in ("verdict.cli", "verdict.commands") or target.startswith(
                    ("verdict.cli.", "verdict.commands.")
                ):
                    rel = filepath.relative_to(ROOT)
                    violations.append(f"{rel}:{lineno} imports {target}")
        assert violations == [], "\n".join(violations)


class TestAliasIdentity:
    """cli.py aliases must be the same objects as the canonical functions."""

    def test_bootstrap_aliases(self) -> None:
        from verdict import cli
        from verdict.actions import helpers

        assert cli._cli_bootstrap is helpers._cli_bootstrap
        assert cli._report_bootstrap_notes is helpers._report_bootstrap_notes
        assert cli._build_route_gate is helpers.build_route_gate

    def test_probe_result_payload_alias(self) -> None:
        from verdict import cli
        from verdict.actions import helpers

        assert cli._probe_result_payload is helpers.probe_result_payload

    def test_default_model_catalog_alias(self) -> None:
        from verdict import cli
        from verdict.actions import helpers

        assert cli.default_model_catalog is helpers.default_model_catalog

    def test_doctor_diagnostics_aliases(self) -> None:
        from verdict import cli
        from verdict import doctor_diagnostics as dd

        assert cli.DoctorDiagnostics is dd.DoctorDiagnostics
        assert cli._collect_doctor_diagnostics is dd._collect_doctor_diagnostics
        assert cli._doctor_gateway_lifecycle is dd._doctor_gateway_lifecycle
        assert cli._gateway_probe_for is dd._gateway_probe_for
        assert cli._doctor_progress is dd._doctor_progress
        assert cli._doctor_fix_gateway is dd._doctor_fix_gateway
        assert cli._doctor_documentation_preflight_is_network_only_failure is (
            dd._doctor_documentation_preflight_is_network_only_failure
        )
        assert cli.DOCTOR_PREFLIGHT_TIMEOUT_DEFAULT is dd.DOCTOR_PREFLIGHT_TIMEOUT_DEFAULT


class TestDoctorJsonUnchanged:
    """doctor --json output must match the canonical structure."""

    @staticmethod
    def _make_mock_diag() -> Any:
        """Build a realistic DoctorDiagnostics mock."""
        from verdict.doctor_diagnostics import DoctorDiagnostics

        diag = DoctorDiagnostics()
        diag.sections.append(("Gateway", "ok", "healthy"))
        diag.issues.clear()
        diag.warnings.append("test-warning")
        diag.fixed.append("test-fix")
        diag.documentation_preflight = {"status": "ok"}
        diag.gateway_lifecycle = {"state": "ready"}
        diag.shared_memory = {}
        diag.capability_report = {"capabilities": []}
        return diag

    def test_doctor_json_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The doctor action produces the expected JSON keys."""
        from verdict.actions.registry import run_action

        mock_diag = self._make_mock_diag()
        monkeypatch.setattr("verdict.cli._collect_doctor_diagnostics", lambda *a, **kw: mock_diag)
        result = run_action("doctor", {"fix": False})
        assert result.ok
        data = result.data
        expected_keys = {
            "status",
            "issues",
            "warnings",
            "repaired",
            "sections",
            "documentation_preflight",
            "gateway_lifecycle",
            "shared_memory",
            "capability_bootstrap",
        }
        assert set(data.keys()) == expected_keys
        assert data["status"] == "ok"
        assert data["issues"] == []
        assert data["warnings"] == ["test-warning"]
        assert data["repaired"] == ["test-fix"]
