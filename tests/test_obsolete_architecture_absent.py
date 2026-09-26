"""obsolete Ruflo/swarm/hivemind architecture is deleted from Core."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from scripts.check_no_obsolete_architecture import find_violations

ROOT = Path(__file__).resolve().parents[1]
VERDICT = ROOT / "verdict"

FORBIDDEN_MODULES = (
    "verdict.hivemind",
    "verdict.hive_workspace",
    "verdict.neural",
    "verdict.sona",
    "verdict.ruvector_adapter",
    "verdict.ruflo_adapter",
    "verdict.ruflo_integration",
    "verdict.ruflo_transport",
    "verdict.ruflo_verification",
    "verdict.swarm",
    "verdict.swarm_contracts",
    "verdict.swarm_dispatcher",
    "verdict.swarm_evidence",
    "verdict.swarm_governance",
    "verdict.swarm_governance_base",
    "verdict.swarm_observability",
    "verdict.swarm_runtime",
    "verdict.swarm_supervisor",
    "verdict.swarm_verification",
    "verdict.lifecycle_controller",
    "verdict.workflow_compiler",
    "verdict.experimental",
)

FORBIDDEN_FILES = (
    "hivemind.py",
    "hive_workspace.py",
    "neural.py",
    "sona.py",
    "ruvector_adapter.py",
    "ruflo_adapter.py",
    "ruflo_integration.py",
    "ruflo_transport.py",
    "ruflo_verification.py",
    "swarm.py",
    "swarm_contracts.py",
    "swarm_dispatcher.py",
    "swarm_evidence.py",
    "swarm_governance.py",
    "swarm_governance_base.py",
    "swarm_observability.py",
    "swarm_runtime.py",
    "swarm_supervisor.py",
    "swarm_verification.py",
    "lifecycle_controller.py",
    "workflow_compiler.py",
)


def test_structural_checker_reports_clean() -> None:
    assert find_violations(VERDICT) == []


def test_forbidden_files_absent() -> None:
    for name in FORBIDDEN_FILES:
        assert not (VERDICT / name).exists(), name
    assert not (VERDICT / "experimental").exists()


@pytest.mark.parametrize("module_name", FORBIDDEN_MODULES)
def test_forbidden_imports_fail(module_name: str) -> None:
    with pytest.raises(ImportError):
        importlib.import_module(module_name)


def test_canonical_memory_and_dispatcher_remain() -> None:
    assert (VERDICT / "dispatcher.py").is_file()
    assert (VERDICT / "memory_plane.py").is_file()
    assert (VERDICT / "memory_migration.py").is_file()
    from verdict.dispatcher import SwarmDispatcher

    assert SwarmDispatcher.__name__ == "SwarmDispatcher"
