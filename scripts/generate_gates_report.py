#!/usr/bin/env python3
"""Derive a release acceptance-gate report from evidence that already exists.

This generator is the missing half of ``scripts/verify_gates.py``: the verifier
validates ``gates_status.json`` but nothing produced one, so the acceptance gates
could never be checked against reality.

The generator observes only.  It never runs a check and never invents a result:

* A test-backed gate is ``PASS`` only when the named test appears in a JUnit XML
  report that CI actually produced and every matching case passed.
* An artifact-backed gate is ``PASS`` only when its named artifact is a real
  regular file in the evidence directory.
* A derived gate is ``PASS`` only when the repository content it inspects
  satisfies the documented condition.

Anything else is ``BLOCKED`` (evidence absent) or ``FAIL`` (evidence present and
negative).  Every gate also gets a note file recording what was inspected, so a
blocked gate still cites real evidence and the verifier can report on it instead
of rejecting a structurally incomplete report.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

SCHEMA_VERSION = 1
REPOSITORY = "mrnicholasbcarter-code/verdict-core"
REPORT_NAME = "gates_status.json"
NOTES_DIR = "notes"
DERIVED_DIR = "derived"
JUNIT_NAME = "pytest_results.xml"

_STEP_START = re.compile(r"^\s*- (name|uses):")

Status = str
DerivedCheck = Callable[[Path], "Derived"]


@dataclass(frozen=True)
class Derived:
    """Outcome of a repository inspection: a status plus the text to record."""

    status: Status
    body: str


@dataclass(frozen=True)
class Gate:
    """One acceptance gate and the evidence that decides it."""

    gate_id: str
    description: str
    tests: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    derived: DerivedCheck | None = None
    derived_name: str = ""


def _read_workflows(repo_root: Path) -> dict[str, str]:
    directory = repo_root / ".github" / "workflows"
    if not directory.is_dir():
        return {}
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.yml"))}


def _workflow_steps(body: str) -> list[str]:
    """Split a workflow into step-sized chunks.

    Advisory markers are per-step, so a whole-file scan would mislabel a workflow
    whose evidence-producing steps are deliberately advisory while its decisive
    step is not.
    """

    steps: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if _STEP_START.match(line):
            if current:
                steps.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        steps.append("\n".join(current))
    return steps


def _is_advisory(step: str) -> bool:
    return "continue-on-error" in step or "|| true" in step


def _check_supply_chain_scans(repo_root: Path) -> Derived:
    """G5.3 - dependency and supply-chain scanning must run, non-advisory."""

    workflows = _read_workflows(repo_root)
    steps = [(name, step) for name, body in workflows.items() for step in _workflow_steps(body)]
    required = {
        "python dependency audit": "pip-audit",
        "npm dependency audit": "npm audit",
        "osv scanner": "osv-scanner",
        "static analysis": "codeql",
    }
    lines = [f"workflows inspected: {', '.join(sorted(workflows)) or '(none)'}"]
    missing: list[str] = []
    advisory: list[str] = []
    for label, needle in required.items():
        matched = [(name, step) for name, step in steps if needle in step.lower()]
        if not matched:
            lines.append(f"MISSING: {label} ({needle})")
            missing.append(label)
            continue
        where = ", ".join(sorted({name for name, _ in matched}))
        lines.append(f"found: {label} ({needle}) in {where}")
        if all(_is_advisory(step) for _, step in matched):
            lines.append(f"  ADVISORY: every {label} step is advisory")
            advisory.append(label)
    if missing:
        return Derived("BLOCKED", "\n".join(lines))
    return Derived("FAIL" if advisory else "PASS", "\n".join(lines))


def _check_gate_workflow(repo_root: Path) -> Derived:
    """G6.4 - a CI workflow must run the verifier, and that step must not be advisory."""

    workflows = _read_workflows(repo_root)
    matched = [
        (name, step)
        for name, body in workflows.items()
        for step in _workflow_steps(body)
        if "verify_gates.py" in step
    ]
    if not matched:
        return Derived("BLOCKED", "no workflow step runs verify_gates.py")
    lines = [f"verifier steps: {', '.join(sorted({name for name, _ in matched}))}"]
    advisory = sorted({name for name, step in matched if _is_advisory(step)})
    lines.append(f"advisory verifier steps: {', '.join(advisory) if advisory else '(none)'}")
    return Derived("FAIL" if advisory else "PASS", "\n".join(lines))


def _check_issue_templates(repo_root: Path) -> Derived:
    """G7.4 - the repository must offer structured issue templates."""

    directory = repo_root / ".github" / "ISSUE_TEMPLATE"
    if not directory.is_dir():
        return Derived("BLOCKED", "no .github/ISSUE_TEMPLATE directory")
    templates = sorted(path.name for path in directory.iterdir() if path.is_file())
    lines = [f"templates: {', '.join(templates) or '(none)'}"]
    return Derived("PASS" if templates else "BLOCKED", "\n".join(lines))


def _check_shared_contracts(repo_root: Path) -> Derived:
    """G4.1 - the client must consume the shared contracts package, not a copy."""

    contracts = repo_root / "contracts" / "package.json"
    client = repo_root / "verdict" / "client-sdk" / "package.json"
    if not contracts.is_file() or not client.is_file():
        return Derived("BLOCKED", "contracts/ or verdict/client-sdk/ package.json is missing")
    contracts_name = json.loads(contracts.read_text(encoding="utf-8")).get("name")
    client_manifest = json.loads(client.read_text(encoding="utf-8"))
    dependencies: dict[str, Any] = {}
    for field in ("dependencies", "peerDependencies", "devDependencies"):
        value = client_manifest.get(field)
        if isinstance(value, dict):
            dependencies.update({key: f"{field}:{spec}" for key, spec in value.items()})
    lines = [
        f"contracts package: {contracts_name}",
        f"client package: {client_manifest.get('name')}",
        f"client reference: {dependencies.get(contracts_name, '(none)')}",
    ]
    status = "PASS" if contracts_name in dependencies else "FAIL"
    return Derived(status, "\n".join(lines))


GATES: tuple[Gate, ...] = (
    Gate(
        "G1.1", "Eligibility filtering runs before ranking", tests=("test_eligibility_runs_first",)
    ),
    Gate(
        "G1.2",
        "Capability, budget, privacy, and capacity floors are enforced",
        tests=(
            "test_capability_floor",
            "test_budget_floor",
            "test_privacy_floor",
            "test_capacity_floor",
        ),
    ),
    Gate(
        "G1.3",
        "Advisory intelligence cannot re-admit a gated candidate",
        tests=("test_intelligence_cannot_bypass_gate",),
    ),
    Gate("G1.4", "Selection is deterministic", tests=("test_deterministic_selection",)),
    Gate("G1.5", "Explain output matches its schema", tests=("test_explain_output_schema",)),
    Gate(
        "G2.1", "Catalog cache honors TTL and stale-while-revalidate", tests=("test_cache_ttl_swr",)
    ),
    Gate("G2.2", "Unknown provider error states are handled", tests=("test_unknown_error_states",)),
    Gate("G2.3", "Concurrent refreshes are deduplicated", tests=("test_refresh_deduplication",)),
    Gate("G2.4", "Cache isolation keys separate tenants", tests=("test_isolation_keys",)),
    Gate(
        "G3.1", "The least-cost eligible candidate is chosen", tests=("test_least_cost_eligible",)
    ),
    Gate("G3.2", "Escalation policy is respected", tests=("test_escalation_policy",)),
    Gate("G3.3", "Budget ceilings are enforced at execution", tests=("test_budget_enforcement",)),
    Gate("G3.4", "Concurrency and timeout bounds hold", tests=("test_concurrency_timeout",)),
    Gate(
        "G4.1",
        "The Node client consumes the shared contracts package",
        derived=_check_shared_contracts,
        derived_name="shared_contracts_usage.txt",
    ),
    Gate(
        "G4.2", "A Python/TypeScript parity matrix exists", artifacts=("contract_parity_matrix.md",)
    ),
    Gate(
        "G4.3",
        "Parity fixtures produce identical results",
        artifacts=("parity_fixture_results.json",),
    ),
    Gate("G4.4", "The Node forwarder test suite passes", artifacts=("forwarder_test_results.xml",)),
    Gate("G5.1", "A threat model is published", artifacts=("THREAT_MODEL.md",)),
    Gate("G5.2", "A privacy policy is published", artifacts=("PRIVACY_POLICY.md",)),
    Gate(
        "G5.3",
        "CI runs non-advisory dependency and supply-chain scans",
        derived=_check_supply_chain_scans,
        derived_name="supply_chain_scan_steps.txt",
    ),
    Gate(
        "G5.4",
        "A committed-credential scan result is recorded",
        artifacts=("secrets_scan_results.txt",),
    ),
    Gate(
        "G6.1",
        "Assignment logs have a schema and a conforming sample",
        artifacts=("assignment_log_schema.json", "assignment_log_sample.json"),
    ),
    Gate("G6.2", "Benchmark results are recorded", artifacts=("benchmark_results.json",)),
    Gate("G6.3", "An evidence bundle manifest is produced", artifacts=("evidence_manifest.json",)),
    Gate(
        "G6.4",
        "CI runs the acceptance-gate verifier non-advisorily",
        derived=_check_gate_workflow,
        derived_name="acceptance_gates_workflow.txt",
    ),
    Gate("G7.1", "The quickstart runs end to end", artifacts=("quickstart_test.log",)),
    Gate("G7.2", "The flagship demo produces output", artifacts=("flagship_demo_output.json",)),
    Gate("G7.3", "README commands are verified", artifacts=("readme_verification.log",)),
    Gate(
        "G7.4",
        "Structured issue templates are available",
        derived=_check_issue_templates,
        derived_name="issue_templates.txt",
    ),
)


def _load_junit(evidence_dir: Path) -> dict[str, list[bool]] | None:
    """Map test name to per-case outcomes, or None when no report was produced."""

    report = evidence_dir / JUNIT_NAME
    if not report.is_file():
        return None
    try:
        tree = ElementTree.parse(report)
    except ElementTree.ParseError:
        return None
    outcomes: dict[str, list[bool]] = {}
    for case in tree.iter("testcase"):
        name = case.get("name") or ""
        base = name.split("[", 1)[0]
        failed = any(child.tag in {"failure", "error", "skipped"} for child in case)
        outcomes.setdefault(base, []).append(not failed)
    return outcomes


def _resolve_tests(gate: Gate, outcomes: dict[str, list[bool]] | None) -> Derived:
    if outcomes is None:
        return Derived(
            "BLOCKED", f"no {JUNIT_NAME} in the evidence directory; no test run observed"
        )
    lines = []
    missing = False
    failed = False
    for test in gate.tests:
        results = outcomes.get(test)
        if results is None:
            lines.append(f"MISSING: {test} (not present in {JUNIT_NAME})")
            missing = True
        elif all(results):
            lines.append(f"pass: {test} ({len(results)} case(s))")
        else:
            lines.append(
                f"FAIL: {test} ({results.count(False)} of {len(results)} case(s) not passing)"
            )
            failed = True
    if failed:
        return Derived("FAIL", "\n".join(lines))
    if missing:
        return Derived("BLOCKED", "\n".join(lines))
    return Derived("PASS", "\n".join(lines))


def _resolve_artifacts(gate: Gate, evidence_dir: Path) -> Derived:
    lines = []
    missing = False
    for artifact in gate.artifacts:
        path = evidence_dir / artifact
        if path.is_file() and not path.is_symlink():
            lines.append(f"present: {artifact} ({path.stat().st_size} bytes)")
        else:
            lines.append(f"MISSING: {artifact}")
            missing = True
    return Derived("BLOCKED" if missing else "PASS", "\n".join(lines))


def _git_commit(repo_root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    if result.returncode != 0 or len(commit) != 40:
        raise SystemExit("cannot determine the repository commit; run inside a git checkout")
    return commit


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def build_report(evidence_dir: Path, repo_root: Path) -> dict[str, Any]:
    """Inspect evidence, write per-gate notes, and return the gate report."""

    notes_dir = evidence_dir / NOTES_DIR
    derived_dir = evidence_dir / DERIVED_DIR
    notes_dir.mkdir(parents=True, exist_ok=True)
    derived_dir.mkdir(parents=True, exist_ok=True)
    outcomes = _load_junit(evidence_dir)

    gates: list[dict[str, Any]] = []
    for gate in GATES:
        evidence: list[str] = []
        if gate.derived is not None:
            result = gate.derived(repo_root)
            derived_path = derived_dir / gate.derived_name
            derived_path.write_text(result.body + "\n", encoding="utf-8")
            evidence.append(f"{DERIVED_DIR}/{gate.derived_name}")
        elif gate.tests:
            result = _resolve_tests(gate, outcomes)
            if outcomes is not None:
                evidence.append(JUNIT_NAME)
        else:
            result = _resolve_artifacts(gate, evidence_dir)
            evidence.extend(
                artifact
                for artifact in gate.artifacts
                if (evidence_dir / artifact).is_file()
                and not (evidence_dir / artifact).is_symlink()
            )

        note = f"{NOTES_DIR}/{gate.gate_id}.md"
        (evidence_dir / note).write_text(
            f"# {gate.gate_id} - {gate.description}\n\n"
            f"Status: {result.status}\n\n"
            "```\n" + result.body + "\n```\n",
            encoding="utf-8",
        )
        evidence.append(note)
        gates.append({"id": gate.gate_id, "status": result.status, "evidence": evidence})

    return {
        "schema_version": SCHEMA_VERSION,
        "repository": REPOSITORY,
        "commit": _git_commit(repo_root),
        "gates": gates,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)

    evidence_dir: Path = args.evidence_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(evidence_dir, args.repo_root)
    (evidence_dir / REPORT_NAME).write_bytes(_canonical_json(report))

    counts = {status: 0 for status in ("PASS", "FAIL", "BLOCKED")}
    for gate in report["gates"]:
        counts[gate["status"]] += 1
    print(
        f"wrote {evidence_dir / REPORT_NAME}: "
        f"{counts['PASS']} pass, {counts['FAIL']} fail, {counts['BLOCKED']} blocked",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
