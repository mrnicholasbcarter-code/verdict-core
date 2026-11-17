"""scripts/check_lint_gates.py runs the same checks as CI's required lint job."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

# A command in a lint-job `run:` block that a local run must mirror.
CI_CHECK = re.compile(
    r"^\s*(?:"
    r"ruff check \.|ruff format --check \.|mypy verdict --strict"
    r"|python (scripts/[\w/]+\.py)"
    r"|npx --yes (@fission-ai/openspec@[\w.]+) validate"
    r")",
    re.MULTILINE,
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_lint_gates", ROOT / "scripts" / "check_lint_gates.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_lint_gates = _load()


def _lint_checks() -> list[str]:
    workflow: dict[str, Any] = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    checks: list[str] = []
    for step in workflow["jobs"]["lint"]["steps"]:
        run = step.get("run", "")
        if "Autofix" in step.get("name", ""):
            continue  # fixes, then re-checks below; the local script only checks
        for match in CI_CHECK.finditer(run):
            token = match.group(1) or match.group(2) or match.group(0).strip()
            if token not in checks:
                checks.append(token)
    return checks


def _local_tokens(wip: bool = False) -> list[str]:
    return [" ".join(g.argv) for g in check_lint_gates.gates("BASE", wip=wip)]


def test_every_ci_lint_check_has_a_local_gate() -> None:
    checks = _lint_checks()
    assert checks, "found no checks in the lint job; has ci.yml moved?"
    local = _local_tokens()
    missing = [c for c in checks if not any(c in cmd for cmd in local)]
    assert not missing, f"ci.yml lint checks missing from check_lint_gates.py: {missing}"


def test_local_gates_run_in_ci_order() -> None:
    local = _local_tokens()
    order = [next(i for i, cmd in enumerate(local) if c in cmd) for c in _lint_checks()]
    assert order == sorted(order)


def test_no_local_gate_that_ci_does_not_run() -> None:
    checks = _lint_checks()
    extra = [cmd for cmd in _local_tokens() if not any(c in cmd for c in checks)]
    assert not extra, f"check_lint_gates.py runs checks ci.yml does not: {extra}"


def test_base_and_wip_are_passed_like_ci() -> None:
    plain = {g.name: " ".join(g.argv) for g in check_lint_gates.gates("BASE", wip=False)}
    assert plain["reachability ratchet"].endswith("--ratchet-base BASE")
    assert plain["proof-path trailers"].endswith("--base BASE")
    assert plain["open OpenSpec tasks"].endswith("--base BASE")
    wip = check_lint_gates.gates("abc", wip=True)[-1]
    assert wip.argv[-2:] == ("abc", "--wip")


def test_unresolvable_base_is_a_usage_error() -> None:
    assert check_lint_gates.main(["--base", "no-such-ref-for-check-lint-gates"]) == 2
