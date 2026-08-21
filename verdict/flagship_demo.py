"""Credential-free, deterministic flagship quickstart fixture.

The fixture uses only in-memory catalog and runtime observations. It never
calls a provider, reads credentials, accesses the network, or writes state.
That makes it safe to run from an installed wheel in an otherwise empty
environment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from verdict.availability import (
    CandidateRequirements,
    RuntimeObservation,
    explain_candidates,
    normalize_observation,
    select_capable_candidates,
)
from verdict.contracts import RoutingDecisionContract, TaskSpec
from verdict.models import ModelInfo
from verdict.security import fingerprint_text
from verdict.trusted_change_report import (
    ACCEPTED_ALL_GATES_GREEN,
    DENIED_FAILED_CHECK,
    assemble_report,
    compute_verdict,
    export_redacted_report,
    stamp_verdict,
)

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def build_demo_result() -> dict[str, Any]:
    """Return the stable, JSON-compatible result used by the quickstart."""

    task_spec = TaskSpec(
        objective="Add structured output to the invoice parser",
        task_type="coding",
        effort="medium",
        reasoning="medium",
        required_capabilities=["tools", "structured_output"],
        tools=["repository", "test_runner"],
        privacy="trusted_upstream",
        risk="high",
        production_impact=False,
        verification={"checks": ["unit_tests", "schema_validation"]},
        metadata={"fixture": "issue-35"},
    )
    candidates = [
        ModelInfo(
            id="demo/frontier-tools",
            provider="demo",
            capability_tier=1,
            capabilities=frozenset({"tools", "structured_output"}),
        ),
        ModelInfo(
            id="demo/no-tools",
            provider="demo",
            capability_tier=1,
            capabilities=frozenset({"structured_output"}),
        ),
        ModelInfo(
            id="demo/quota-empty",
            provider="demo",
            capability_tier=0,
            capabilities=frozenset({"tools", "structured_output"}),
        ),
        ModelInfo(
            id="demo/unverified",
            provider="demo",
            capability_tier=0,
            capabilities=frozenset({"tools", "structured_output"}),
        ),
    ]
    observations = {
        "demo/frontier-tools": RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=80
        ),
        "demo/no-tools": RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=80
        ),
        "demo/quota-empty": RuntimeObservation(
            observed_at=NOW, source="fixture", health="healthy", quota_remaining_pct=0
        ),
        "demo/unverified": RuntimeObservation(observed_at=NOW, source="fixture", health="unknown"),
    }
    states = [normalize_observation(model, observations[model.id], now=NOW) for model in candidates]
    requirements = CandidateRequirements(
        required=frozenset(task_spec.required_capabilities), protected=True
    )
    eligible = select_capable_candidates(states, requirements)
    explanation = explain_candidates(states, requirements)
    selected = eligible[0].model.id if eligible else None
    decision = RoutingDecisionContract(
        selected_route={"runtime_id": selected, "provider": "demo"},
        task_spec=task_spec.to_dict(),
        candidate_snapshot="fixture:issue-35",
        exclusions=[row for row in explanation if row["rejected"]],
        policy_floor="high",
        planner_mode="deterministic_fixture",
        explanation=(
            "Selected the only candidate satisfying required capabilities and "
            "fresh healthy availability; excluded all hard-gate failures."
        ),
        fallback_plan=[],
        policy_version="demo-policy-1",
    )
    return {
        "task_spec": task_spec.to_dict(),
        "requirements": {
            "required": sorted(requirements.required),
            "protected": requirements.protected,
        },
        "eligible": [item.model.id for item in eligible],
        "candidates": explanation,
        "decision": decision.to_dict(),
    }


def validate_demo_result(result: dict[str, Any]) -> None:
    """Fail closed if the quickstart fixture no longer proves its contract."""

    if result.get("eligible") != ["demo/frontier-tools"]:
        raise ValueError("quickstart fixture selected an unexpected route")
    selected = result.get("decision", {}).get("selected_route", {}).get("runtime_id")
    if selected != "demo/frontier-tools":
        raise ValueError("quickstart fixture decision is inconsistent")
    exclusions = result.get("decision", {}).get("exclusions")
    if not isinstance(exclusions, list) or len(exclusions) != 3:
        raise ValueError("quickstart fixture did not record all exclusions")


def run_demo() -> dict[str, Any]:
    """Build and validate one deterministic quickstart result."""

    result = build_demo_result()
    validate_demo_result(result)
    return result


def render_report(result: dict[str, Any]) -> str:
    """Render a stable human-readable report without terminal-specific markup."""

    decision = result["decision"]
    lines = [
        "Verdict credential-free quickstart",
        "===================================",
        f"Task: {result['task_spec']['objective']}",
        f"Required capabilities: {', '.join(result['requirements']['required'])}",
        f"Selected route: {decision['selected_route']['runtime_id']}",
        f"Excluded candidates: {len(decision['exclusions'])}",
        "Status: PASS",
    ]
    for exclusion in decision["exclusions"]:
        lines.append(f"- {exclusion['model']}: {exclusion['reason']}")
    return "\n".join(lines) + "\n"


def build_trusted_change_report_demo() -> dict[str, Any]:
    """Credential-free Trusted Change Report quickstart (feature 002).

    Projects the existing ``build_demo_result()`` routing decision, a synthetic
    source state, deterministic verification results, and an evidence receipt
    into a ``TrustedChangeReport`` via the carrier pipeline
    (``assemble_report`` → ``compute_verdict`` → ``export_redacted_report``).

    No network, no credentials, no ``time.time()``: every timestamp and id is a
    fixed constant so the demo reproduces byte-for-byte from an installed wheel.
    The carrier NEVER recomputes eligibility (FR-010); it merely projects the
    already-decided route and gates into a source-bound report and a fail-closed
    acceptance verdict.
    """

    result = build_demo_result()
    decision = result["decision"]  # already a dict in the demo fixture

    # Synthetic source state bound to a fixed commit (the demo doesn't have a
    # real checkout to bind to; it pins a stable placeholder hash instead).
    source_state = {
        "repository_url": "demo-repository",
        "source_state_id": "ss-demo-accepted-0001",
        "commit_sha": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        "commit_message": "ship widget v2 endpoint",
        "commit_author": "release-bot",
        "commit_timestamp": "2026-07-16T12:00:00Z",
        "branch": "main",
        "dirty_files": [],
        "untracked_files": [],
        "submodule_states": {},
        "worktree_path": "<installed-wheel>",
        "snapshot_timestamp": "2026-07-16T12:00:00Z",
        "snapshot_method": "clean_commit",
    }

    # Deterministic verification results: the focused test gate passed.
    verification_results: list[dict[str, Any]] = [
        {
            "check_name": "focused-tests",
            "check_type": "focused_tests",
            "status": "passed",
            "command": "verdict check",
            "runtime": "python",
            "provenance": "flagship-demo",
            "policy_requirement": "all-tests-pass",
            "raw_output": "ok",
        },
        {
            "check_name": "diff-boundary",
            "check_type": "diff_boundary",
            "status": "passed",
            "command": "verdict check-boundary",
            "runtime": "python",
            "provenance": "flagship-demo",
            "policy_requirement": "no-protected-files",
            "raw_output": "no violations",
        },
    ]

    diff_summary = {
        "files_changed": ["verdict/widget_v2.py"],
        "lines_added": 42,
        "lines_removed": 3,
        "protected_files_touched": [],
        "boundary_violations": [],
        "diff_digest": "sha256:" + "a" * 64,
    }

    receipt_payload = {"objective": result["task_spec"]["objective"]}
    receipts = [
        {
            "payload": receipt_payload,
            "hash": fingerprint_text('{"objective": "ship widget v2 endpoint"}'),
            "integrity_ok": True,
            "captured_at": "2026-07-16T12:00:00Z",
            "receipt_id": "er-sha256:00000000000",
        }
    ]

    report = assemble_report(
        objective=result["task_spec"]["objective"],
        task_type=result["task_spec"]["task_type"],
        work_unit_ids=["demo-wu-1"],
        route_decision=decision,
        eligibility={},
        receipts=receipts,
        verification_results=verification_results,
        diff_summary=diff_summary,
        source_state=source_state,
        received_at="2026-07-16T12:00:00Z",
        generated_at="2026-07-16T12:00:01Z",
        report_id="tcr-demo-accepted-0001",
    )

    # Stamp the fail-closed verdict into the report before exporting, so the
    # portable report presents the computed decision, not the interim unknown.
    report = stamp_verdict(report)
    verdict = compute_verdict(report)
    redacted = export_redacted_report(report)

    validate_trusted_change_report_demo(verdict.decision, verdict.reason)
    return {
        "report": report.to_dict(),
        "verdict": {"decision": verdict.decision, "reason": verdict.reason},
        "redacted": redacted,
    }


def validate_trusted_change_report_demo(decision: str, reason: str) -> None:
    """Fail closed if the Trusted Change Report demo no longer proves its contract."""

    if decision != "accepted":
        raise ValueError(
            f"trusted-change-report demo must accept an all-gates-green change; got {decision!r}"
        )
    if reason != ACCEPTED_ALL_GATES_GREEN:
        raise ValueError(f"unexpected verdict reason: {reason!r}")


def build_denied_trusted_change_report_demo() -> dict[str, Any]:
    """Credential-free Trusted Change Report quickstart — the DENIED leg (feature 005).

    Mirrors :func:`build_trusted_change_report_demo` (the accepted leg) but projects a
    **failing** focused-test verification gate into the same carrier pipeline
    (``assemble_report`` → ``stamp_verdict`` → ``export_redacted_report``). The carrier
    never recomputes eligibility (FR-010): it only projects the already-healthy route
    plus the failed gate, and :func:`compute_verdict` returns
    ``decision="denied"``, ``reason="DENIED_FAILED_CHECK"``.

    The denial is a verification-gate failure — the most honest and reproducible
    "denied change" scenario. The routing decision is reused unchanged from
    :func:`build_demo_result` so the route stays eligible; the verdict diverges
    purely from the projected gate. No network, no credentials, no ``time.time()``:
    every timestamp and id is a fixed constant so the denied leg — like the
    accepted leg — reproduces byte-for-byte from an installed wheel (SC-002).

    Together with the accepted leg, this proves gate 1 of the release gate: "an
    accepted change AND a denied change".
    """

    result = build_demo_result()
    decision = result["decision"]

    # Distinct demo source/report ids keep the denied leg's digest independent of the
    # accepted leg's. The commit is a fixed constant (fail-closed source binding).
    source_state = {
        "repository_url": "demo-repository",
        "source_state_id": "ss-demo-denied-0001",
        "commit_sha": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3",
        "commit_message": "ship widget v2 endpoint (broken focus)",
        "commit_author": "release-bot",
        "commit_timestamp": "2026-07-16T12:30:00Z",
        "branch": "main",
        "dirty_files": [],
        "untracked_files": [],
        "submodule_states": {},
        "worktree_path": "<installed-wheel>",
        "snapshot_timestamp": "2026-07-16T12:30:00Z",
        "snapshot_method": "clean_commit",
    }

    # One required check FAILED. compute_verdict's rule order returns DENIED_FAILED_CHECK
    # for the first status=="failed" verification result (after the source-binding check).
    verification_results: list[dict[str, Any]] = [
        {
            "check_name": "focused-tests",
            "check_type": "focused_tests",
            "status": "failed",
            "command": "verdict check",
            "runtime": "python",
            "provenance": "flagship-demo",
            "policy_requirement": "all-tests-pass",
            "raw_output": "FAILED tests/test_widget_v2.py::test_endpoint - 1 failed",
        }
    ]

    # Clean diff summary — no boundary violations — so the denial is solely the failed
    # gate, not an out-of-scope change. This keeps the scenario honest: the gate failed.
    diff_summary = {
        "files_changed": ["verdict/widget_v2.py"],
        "lines_added": 42,
        "lines_removed": 3,
        "protected_files_touched": [],
        "boundary_violations": [],
        "diff_digest": "sha256:" + "b" * 64,
    }

    receipt_payload = {"objective": result["task_spec"]["objective"]}
    receipts = [
        {
            "payload": receipt_payload,
            "hash": fingerprint_text('{"objective": "ship widget v2 endpoint"}'),
            "integrity_ok": True,
            "captured_at": "2026-07-16T12:30:00Z",
            "receipt_id": "er-sha256:00000000001",
        }
    ]

    report = assemble_report(
        objective=result["task_spec"]["objective"],
        task_type=result["task_spec"]["task_type"],
        work_unit_ids=["demo-wu-1"],
        route_decision=decision,
        eligibility={},
        receipts=receipts,
        verification_results=verification_results,
        diff_summary=diff_summary,
        source_state=source_state,
        received_at="2026-07-16T12:30:00Z",
        generated_at="2026-07-16T12:30:01Z",
        report_id="tcr-demo-denied-0001",
    )

    report = stamp_verdict(report)
    verdict = compute_verdict(report)
    redacted = export_redacted_report(report)

    validate_trusted_change_report_denied_demo(verdict.decision, verdict.reason)
    return {
        "report": report.to_dict(),
        "verdict": {"decision": verdict.decision, "reason": verdict.reason},
        "redacted": redacted,
    }


def validate_trusted_change_report_denied_demo(decision: str, reason: str) -> None:
    """Fail closed if the denied Trusted Change Report demo no longer proves its contract.

    The denied leg MUST present a ``denied`` decision with the
    :data:`~verdict.trusted_change_report.DENIED_FAILED_CHECK` reason: a change whose
    projected verification gate genuinely failed. Any drift (e.g. the leg accidentally
    returning ``accepted``) raises so the demo never silently ships a false negative.
    """

    if decision != "denied":
        raise ValueError(
            f"trusted-change-report denied demo must DENY a failed-gate change; got {decision!r}"
        )
    if reason != DENIED_FAILED_CHECK:
        raise ValueError(f"unexpected denied verdict reason: {reason!r}")


def run_accepted_and_denied_demo() -> dict[str, Any]:
    """Produce BOTH demo legs in one credential-free call (gate 1 of the release gate).

    Returns::

        {
            "accepted": <build_trusted_change_report_demo() result>,
            "denied":   <build_denied_trusted_change_report_demo() result>,
        }

    Both legs are deterministic and source-bound; running this once is the release-gate
    assertion that "an accepted change AND a denied change reproduce credential-free."
    This is the accepted-and-denied gate-1 proof tracked within verdict-core #266.
    """

    return {
        "accepted": build_trusted_change_report_demo(),
        "denied": build_denied_trusted_change_report_demo(),
    }


def main() -> None:
    """Print the installed-wheel accepted-and-denied demo as deterministic JSON."""

    # The fixture is intentionally credential-free; CodeQL's taint model does not
    # distinguish this constant-data demo from producer-controlled report content.
    # lgtm[py/clear-text-logging-sensitive-data] - fixed credential-free fixture
    print(json.dumps(run_accepted_and_denied_demo(), indent=2, sort_keys=True))


__all__ = [
    "build_demo_result",
    "build_denied_trusted_change_report_demo",
    "build_trusted_change_report_demo",
    "main",
    "render_report",
    "run_accepted_and_denied_demo",
    "run_demo",
    "validate_demo_result",
    "validate_trusted_change_report_demo",
    "validate_trusted_change_report_denied_demo",
]


if __name__ == "__main__":
    main()
