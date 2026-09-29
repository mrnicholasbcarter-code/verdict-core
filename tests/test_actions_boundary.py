"""Tests for BOD-275 Lane A: dependency direction boundary enforcement.

Checks that:
1. verdict/actions/**/*.py never imports verdict.cli or verdict.commands
2. cli.py aliases are identical objects to the canonical functions
3. doctor --json output is unchanged
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, ClassVar

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
                if target in (
                    "verdict.cli",
                    "verdict.commands",
                    "verdict.orchestration.cli",
                ) or target.startswith(
                    ("verdict.cli.", "verdict.commands.", "verdict.orchestration.cli.")
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
        monkeypatch.setattr(
            "verdict.doctor_diagnostics._collect_doctor_diagnostics", lambda *a, **kw: mock_diag
        )
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


class TestNoCliStringReferences:
    """AST scan: verdict/actions/** and verdict/doctor_diagnostics.py must not reference
    'verdict.cli' or 'verdict.commands' as strings or via sys.modules subscripts."""

    FORBIDDEN = ("verdict.cli", "verdict.commands", "verdict.orchestration.cli")
    SCAN_PATHS: ClassVar[list[Path]] = [
        ROOT / "verdict" / "actions",
        ROOT / "verdict" / "doctor_diagnostics.py",
    ]

    @staticmethod
    def _collect_files() -> list[Path]:
        files: list[Path] = []
        for p in TestNoCliStringReferences.SCAN_PATHS:
            if p.is_dir():
                files.extend(sorted(p.rglob("*.py")))
            elif p.is_file():
                files.append(p)
        return files

    @staticmethod
    def _scan_for_cli_strings(filepath: Path) -> list[str]:
        """Find string constants or sys.modules subscripts referencing verdict.cli."""
        source = filepath.read_text()
        tree = ast.parse(source, filename=str(filepath))
        violations: list[str] = []
        rel = filepath.relative_to(ROOT)
        for node in ast.walk(tree):
            # Check string constants
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for forbidden in TestNoCliStringReferences.FORBIDDEN:
                    if forbidden in node.value:
                        violations.append(
                            f"{rel}:{node.lineno} string constant contains '{forbidden}'"
                        )
            # Check sys.modules['verdict.cli'] subscripts
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id in ("sys", "_sys")
                and node.value.attr == "modules"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                for forbidden in TestNoCliStringReferences.FORBIDDEN:
                    if forbidden in node.slice.value:
                        violations.append(
                            f"{rel}:{node.lineno} sys.modules subscript references '{forbidden}'"
                        )
            # Check Import/ImportFrom
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for forbidden in TestNoCliStringReferences.FORBIDDEN:
                        if alias.name == forbidden or alias.name.startswith(forbidden + "."):
                            violations.append(f"{rel}:{node.lineno} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for forbidden in TestNoCliStringReferences.FORBIDDEN:
                    if module == forbidden or module.startswith(forbidden + "."):
                        violations.append(f"{rel}:{node.lineno} imports from {module}")
        return violations

    def test_no_cli_references_in_actions_and_doctor_diagnostics(self) -> None:
        """No file under verdict/actions/ or verdict/doctor_diagnostics.py references verdict.cli."""
        violations: list[str] = []
        for filepath in self._collect_files():
            violations.extend(self._scan_for_cli_strings(filepath))
        assert violations == [], "\n".join(violations)


class TestDoctorActionIndependentOfCli:
    """Behaviour test: run_action('doctor') must not call cli's copy of _collect_doctor_diagnostics."""

    def test_doctor_action_ignores_cli_copy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With verdict.cli imported and its _collect_doctor_diagnostics replaced by a
        function that raises, run_action('doctor') still works — it uses the canonical
        implementation in verdict.doctor_diagnostics, not the cli's copy."""
        # Ensure verdict.cli is imported so sys.modules['verdict.cli'] is populated
        import verdict.cli  # noqa: F401
        from verdict.actions.registry import run_action
        from verdict.doctor_diagnostics import DoctorDiagnostics

        # Replace cli's copy with one that raises
        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("Must not call cli._collect_doctor_diagnostics")

        monkeypatch.setattr("verdict.cli._collect_doctor_diagnostics", _boom)

        # Build a realistic mock diag
        diag = DoctorDiagnostics()
        diag.sections.append(("Gateway", "ok", "healthy"))
        diag.issues.clear()
        diag.warnings.clear()
        diag.fixed.clear()
        diag.documentation_preflight = {"status": "ok"}
        diag.gateway_lifecycle = {"state": "ready"}
        diag.shared_memory = {}
        diag.capability_report = {"capabilities": []}

        # Patch the canonical implementation
        monkeypatch.setattr(
            "verdict.doctor_diagnostics._collect_doctor_diagnostics", lambda *a, **kw: diag
        )

        result = run_action("doctor", {"fix": False})
        assert result.ok
        assert result.data["status"] == "ok"
