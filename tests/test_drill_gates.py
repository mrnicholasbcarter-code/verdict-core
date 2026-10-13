"""The scheduled gate drills keep every "stop false done" gate honest."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location("drill_gates", ROOT / "scripts/drill_gates.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_drill_names_a_gate_the_ci_lint_job_runs() -> None:
    drills = _load()
    lint = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = " ".join(str(step.get("run", "")) for step in lint["jobs"]["lint"]["steps"])
    gates = drills._gates("openspec", "BASE")
    for drill in drills.DRILLS:
        script = next(
            part for part in gates[drill.gate] if part.endswith(".py") or part == "validate"
        )
        assert script.rsplit("/", 1)[-1] in steps, drill.name


def test_gate_drills_workflow_is_scheduled() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/gate-drills.yml").read_text())
    assert "schedule" in workflow[True]
    assert "scripts/drill_gates.py" in str(workflow["jobs"]["gate-drills"]["steps"])
