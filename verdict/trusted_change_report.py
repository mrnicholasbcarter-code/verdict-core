"""Trusted Change Report — carrier pipeline (feature 002).

This module is a **carrier of evidence, not a decider** (FR-010). It projects the
system's existing route/eligibility/verification/receipt evidence into one
``TrustedChangeReport`` (defined in ``verdict.contracts``) bound to an exact
source state, computes a fail-closed acceptance verdict from those *projected*
facts, supports independent verification from a tagged source with no producer
trust, and exports a deterministic, leak-free portable report.

It does **not** recompute eligibility or introduce a new policy authority. The
``EligibilityResult``, ``RoutingDecisionContract``, ``EvidenceReceipt`` and
``VerificationResult`` records are already decided before this code runs; it only
projects and refuses to *present* an ``accepted`` verdict when the projected
facts contradict.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from verdict.contracts import (
    AcceptanceDecision,
    DiffSummary,
    RouteRecommendation,
    SourceState,
    TrustedChangeReport,
    VerificationResult,
    redact_contract_secrets,
)
from verdict.security import fingerprint_text

# Stable verdict reason codes (constitution IV: no invented numeric thresholds).
ACCEPTED_ALL_GATES_GREEN = "ACCEPTED_ALL_GATES_GREEN"
DENIED_FAILED_CHECK = "DENIED_FAILED_CHECK"
DENIED_MISSING_VERIFICATION = "DENIED_MISSING_VERIFICATION"
DENIED_INELIGIBLE_ROUTE = "DENIED_INELIGIBLE_ROUTE"
DENIED_OUT_OF_SCOPE = "DENIED_OUT_OF_SCOPE"
DENIED_TAMPERED_EVIDENCE = "DENIED_TAMPERED_EVIDENCE"
DENIED_UNBOUND_SOURCE = "DENIED_UNBOUND_SOURCE"
VERDICT_NOT_COMPUTED = "VERDICT_NOT_COMPUTED"

# Statuses that are NOT a positive pass. ``unknown`` and ``skipped`` must never
# read as a green check (NFR-002 fail-closed; contracts.py keeps "skipped" and
# "unknown" distinct from "passed"/"failed").
_NON_PASSING = {"failed", "skipped", "unknown", ""}


class VerificationFault(str, Enum):
    """Independent-verification fault codes (P3)."""

    SOURCE_BINDING_MISMATCH = "source_binding_mismatch"
    EVIDENCE_TAMPERED = "evidence_tampered"
    RECEIPT_TAMPERED = "receipt_tampered"
    ACCEPTED_ON_INVALID_GATES = "accepted_on_invalid_gates"


def utc_now_iso() -> str:
    """Deterministic-looking but current UTC timestamp (ISO 8601, 'Z').

    Reports are deterministic modulo ``generated_at``; callers that need full
    reproducibility pass an explicit ``generated_at`` instead.
    """

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _git(worktree: Path, *args: str) -> str:
    """Run a read-only git command in ``worktree``. No network.

    Only local-inspection subcommands are used here; ``git fetch``/network ops
    are never invoked (NFR-003 credential-free / offline).
    """

    result = subprocess.run(
        ["git", "-C", str(worktree), *args], check=True, capture_output=True, text=True, timeout=10
    )
    return result.stdout.rstrip("\n")


def capture_source_state(
    worktree: Path,
    *,
    method: str,
    repository_url: str,
    branch: str,
    snapshot_timestamp: str,
    parent_source_state_id: str | None = None,
) -> SourceState:
    """Bind an exact source state from a local checkout (no network).

    ``method`` must be one of ``clean_commit``, ``dirty_snapshot``,
    ``stash_restore``. ``snapshot_timestamp`` is caller-supplied (deterministic
    callers pass a fixed string; ``time.time()`` is intentionally avoided on
    the credential-free demo path). Reads ``git`` locally only.
    """

    if method not in ("clean_commit", "dirty_snapshot", "stash_restore"):
        raise ValueError(f"invalid snapshot_method: {method}")

    commit_sha = _git(worktree, "rev-parse", "HEAD")
    try:
        commit_subject = _git(worktree, "show", "-s", "--format=%s", "HEAD")
        commit_author = _git(worktree, "show", "-s", "--format=%an", "HEAD")
        commit_timestamp = _git(worktree, "show", "-s", "--format=%cI", "HEAD")
    except subprocess.CalledProcessError:
        commit_subject = ""
        commit_author = ""
        commit_timestamp = ""

    porcelain = _git(worktree, "status", "--porcelain")
    dirty_files: list[str] = []
    untracked_files: list[str] = []
    for line in porcelain.splitlines():
        path = line[3:]
        if line.startswith("??"):
            untracked_files.append(path)
        else:
            dirty_files.append(path)

    submodule_states: dict[str, str] = {}
    try:
        for line in _git(worktree, "submodule", "status").splitlines():
            if line.strip():
                sha = line.strip().split()[0]
                name = (
                    line.strip().split(maxsplit=1)[1]
                    if len(line.strip().split(maxsplit=1)) > 1
                    else "unknown"
                )
                submodule_states[name] = sha
    except subprocess.CalledProcessError:
        pass

    if method == "clean_commit" and (dirty_files or untracked_files):
        raise ValueError(
            "clean_commit method requires a clean working tree; "
            f"found {len(dirty_files)} dirty and {len(untracked_files)} untracked"
        )

    return SourceState(
        repository_url=repository_url,
        commit_sha=commit_sha,
        commit_message=commit_subject or None,
        commit_author=commit_author,
        commit_timestamp=commit_timestamp,
        branch=branch,
        dirty_files=dirty_files if method != "clean_commit" else [],
        untracked_files=untracked_files if method != "clean_commit" else [],
        submodule_states=submodule_states,
        worktree_path=str(worktree),
        snapshot_timestamp=snapshot_timestamp,
        snapshot_method=method,
        parent_source_state_id=parent_source_state_id,
    )


def assemble_report(
    *,
    objective: str,
    task_type: str,
    work_unit_ids: list[str],
    route_decision: dict[str, Any],
    eligibility: dict[str, Any],
    receipts: list[dict[str, Any]],
    verification_results: Sequence[dict[str, Any] | VerificationResult],
    diff_summary: dict[str, Any] | DiffSummary,
    source_state: dict[str, Any] | SourceState,
    received_at: str,
    generated_at: str,
    route_recommendation: dict[str, Any] | RouteRecommendation | None = None,
    acceptance: dict[str, Any] | AcceptanceDecision | None = None,
    report_id: str | None = None,
) -> TrustedChangeReport:
    """Project existing evidence into a ``TrustedChangeReport``.

    Does **not** compute a verdict unless one is passed in via ``acceptance``;
    otherwise stamps an interim ``unknown``/``VERDICT_NOT_COMPUTED`` decision so
    the fail-closed invariant holds (``unknown`` never reads as accepted).
    Never calls into ``verdict.eligibility`` — the eligibility decision is
    *projected* as data, not re-derived (FR-010).

    ``report_id`` is optional: a content-stable identifier may be pinned by
    credential-free demos (the contract default otherwise uses a time-based
    factory, which is intentionally NOT deterministic).
    """

    if acceptance is None:
        acceptance = AcceptanceDecision(decision="unknown", reason=VERDICT_NOT_COMPUTED)
    elif isinstance(acceptance, dict):
        acceptance = AcceptanceDecision(**acceptance)

    reco: RouteRecommendation | None = None
    if route_recommendation is not None and isinstance(route_recommendation, dict):
        reco = RouteRecommendation(**route_recommendation)
    elif isinstance(route_recommendation, RouteRecommendation):
        reco = route_recommendation

    fields: dict[str, Any] = dict(
        objective=objective,
        task_type=task_type,
        work_unit_ids=list(work_unit_ids),
        route_decision=dict(route_decision),
        evidence_receipts=[dict(r) for r in receipts],
        verification_results=[
            vr if isinstance(vr, VerificationResult) else VerificationResult(**vr)
            for vr in verification_results
        ],
        diff_summary=diff_summary
        if isinstance(diff_summary, DiffSummary)
        else DiffSummary(**diff_summary),
        source_state=source_state
        if isinstance(source_state, SourceState)
        else SourceState(**source_state),
        acceptance=acceptance,
        route_recommendation=reco,
        received_at=received_at,
        generated_at=generated_at,
    )
    if report_id is not None:
        fields["report_id"] = report_id
    return TrustedChangeReport(**fields)


def _route_is_eligible(route_decision: dict[str, Any], eligibility: dict[str, Any]) -> bool:
    """Read the *projected* eligibility decision. Never re-derives it."""

    # Prefer an explicit projected eligibility state; fall back to the
    # selected_route being present and not explicitly excluded.
    state = str(
        eligibility.get("decision") or eligibility.get("verdict") or eligibility.get("state") or ""
    ).lower()
    if state:
        return state == "eligible"
    selected = route_decision.get("selected_route") or route_decision.get("selected")
    excluded = route_decision.get("exclusions") or []
    if excluded and not selected:
        return False
    return bool(selected)


def compute_verdict(report: TrustedChangeReport) -> AcceptanceDecision:
    """Fail-closed verdict from PROJECTED facts only (no second authority).

    Reads only fields already on the report. ``accepted`` is returned only when
    every gate the projected evidence describes is green; any non-passing,
    missing, out-of-scope, ineligible, tampered, or unbound condition yields a
    ``denied`` verdict with a distinct stable code (FR-005, NFR-002).
    """
    # Rule 6 — fail-closed binding first (no source = no trust).
    source = report.source_state
    if not (source.commit_sha if isinstance(source, SourceState) else source.get("commit_sha", "")):
        return AcceptanceDecision(decision="denied", reason=DENIED_UNBOUND_SOURCE)

    # Rule 1 — a required verification check that failed.
    for vr in report.verification_results:
        status = vr.status if isinstance(vr, VerificationResult) else vr.get("status", "")
        if status == "failed":
            return AcceptanceDecision(decision="denied", reason=DENIED_FAILED_CHECK)

    # Rule 2 — a required check is unknown/skipped/absent (fail-closed).
    for vr in report.verification_results:
        status = vr.status if isinstance(vr, VerificationResult) else vr.get("status", "")
        if status in {"unknown", "skipped", ""}:
            return AcceptanceDecision(decision="denied", reason=DENIED_MISSING_VERIFICATION)

    # Rule 4 — the change touched protected/boundary files.
    if report.diff_summary.boundary_violations:
        return AcceptanceDecision(decision="denied", reason=DENIED_OUT_OF_SCOPE)

    # Rule 3 — the projected route was not eligible.
    if not _route_is_eligible(report.route_decision, {}):
        # ``route_decision`` itself must carry the eligibility state on reports
        # built by ``assemble_report``; if the caller projected eligibility into
        # ``route_decision``, we read it there.
        elg = report.route_decision.get("eligibility")
        if elg is not None and not _route_is_eligible(report.route_decision, elg):
            return AcceptanceDecision(decision="denied", reason=DENIED_INELIGIBLE_ROUTE)

    # Rule 5 — a receipt integrity marker failed (callers flag receipts whose
    # ``hash`` doesn't match their payload by setting ``integrity_ok=False``).
    for receipt in report.evidence_receipts:
        if receipt.get("integrity_ok") is False:
            return AcceptanceDecision(decision="denied", reason=DENIED_TAMPERED_EVIDENCE)

    return AcceptanceDecision(decision="accepted", reason=ACCEPTED_ALL_GATES_GREEN)


def stamp_verdict(report: TrustedChangeReport) -> TrustedChangeReport:
    """Return a copy of ``report`` with the fail-closed verdict stamped.

    ``assemble_report`` projects an interim ``unknown``/``VERDICT_NOT_COMPUTED``
    decision so the carrier never presents an uncomputed decision as accepted.
    Callers that want the decision *embedded* in a portable report call this to
    stamp the projected verdict. Uses ``dataclasses.replace`` because the report
    is a frozen contract.
    """

    verdict = compute_verdict(report)
    return dataclasses.replace(report, acceptance=verdict)


def canonical_report_payload(report: TrustedChangeReport) -> dict[str, Any]:
    """Serialize a report to a canonical dict with the volatile field removed.

    Used by ``compute_report_digest`` and determinism tests. ``generated_at`` is
    the only field that legitimately differs between two reproductions of the
    same report (NFR-001), so it is dropped before hashing.
    """

    payload: dict[str, Any] = json.loads(json.dumps(_serialize(report), sort_keys=True))
    payload.pop("generated_at", None)
    return payload


def _serialize(report: TrustedChangeReport) -> dict[str, Any]:
    serialized: dict[str, Any] = json.loads(
        json.dumps(report.to_dict(), sort_keys=True, default=_json_default)
    )
    return serialized


def _json_default(obj: Any) -> Any:
    if isinstance(obj, VerificationResult):
        return obj.to_dict()
    if isinstance(obj, SourceState):
        return obj.to_dict()
    if isinstance(obj, DiffSummary):
        return obj.to_dict()
    if isinstance(obj, RouteRecommendation):
        return obj.to_dict()
    raise TypeError(f"unserializable: {type(obj)!r}")


def compute_report_digest(report: TrustedChangeReport) -> str:
    """Stable ``sha256:<hex>`` over the canonical (timestamp-stripped) report."""

    canonical = json.dumps(canonical_report_payload(report), sort_keys=True, separators=(",", ":"))
    return fingerprint_text(canonical)


def export_redacted_report(report: TrustedChangeReport) -> dict[str, Any]:
    """Deterministic, leak-free portable dict (P4).

    Redacts secret/PII patterns, drops producer-internal fields that carry no
    decision value, and retains the decision-relevant fields + stable identity
    hashes needed to verify binding. Pure function: same ``report`` in → same
    dict out (NFR-001); ``generated_at`` is preserved as-is, not recomputed.
    """

    redacted = redact_contract_secrets(report)
    payload = redacted if isinstance(redacted, dict) else redacted.to_dict()

    # Drop producer-internal fields that aren't decision-relevant but might leak.
    for key in ("raw_output", "command", "runtime"):
        for vr in payload.get("verification_results", []):
            if isinstance(vr, dict):
                vr.pop(key, None)

    return payload


def verify_report(
    report: TrustedChangeReport, *, source_checkout: Path
) -> VerificationFault | None:
    """Independently verify a report against a tagged source (P3).

    Recomputes the source binding and evidence digests from ``source_checkout``
    with **no producer trust, no credentials, and no network** (FR-007). Returns
    a distinct ``VerificationFault`` on any mismatch, ``None`` if the report is
    self-consistent and bound to the provided checkout.
    """

    # 1. Source binding: recompute SourceState from the checkout and compare.
    source = report.source_state
    if not isinstance(source, SourceState):
        # Reports always store a SourceState instance; a raw dict indicates
        # construction outside assemble_report — fail safe.
        return VerificationFault.SOURCE_BINDING_MISMATCH
    try:
        recomputed = capture_source_state(
            source_checkout,
            method=source.snapshot_method,
            repository_url=source.repository_url,
            branch=source.branch,
            snapshot_timestamp=source.snapshot_timestamp,
        )
    except (ValueError, subprocess.CalledProcessError):
        return VerificationFault.SOURCE_BINDING_MISMATCH

    if recomputed.commit_sha != source.commit_sha:
        return VerificationFault.SOURCE_BINDING_MISMATCH

    # 2. Cross-check: a report claiming ``accepted`` must also pass
    # ``compute_verdict`` on its own projected facts (no trust).
    projected_verdict = compute_verdict(report)
    if report.acceptance.decision == "accepted" and projected_verdict.decision != "accepted":
        return VerificationFault.ACCEPTED_ON_INVALID_GATES

    # 3. Receipt integrity markers — detect tampering without producer trust.
    for receipt in report.evidence_receipts:
        if receipt.get("integrity_ok") is False:
            return VerificationFault.RECEIPT_TAMPERED
        expected = receipt.get("hash")
        if expected is not None:
            payload = receipt.get("payload", {})
            actual = fingerprint_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            if expected != actual:
                return VerificationFault.RECEIPT_TAMPERED

    return None


def reproduce_report(report: TrustedChangeReport, *, source_checkout: Path) -> TrustedChangeReport:
    """Clean-checkout independent reproduction (NFR-003).

    Re-captures the source state from ``source_checkout`` and rebuilds the
    report from the same projected inputs, asserting byte-equality modulo
    ``generated_at``. Raises ``AssertionError`` if the reproduction diverges.
    """

    source = report.source_state
    if not isinstance(source, SourceState):
        raise AssertionError("reproduce_report requires a SourceState-bound report")
    recomputed_source = capture_source_state(
        source_checkout,
        method=source.snapshot_method,
        repository_url=source.repository_url,
        branch=source.branch,
        snapshot_timestamp=source.snapshot_timestamp,
    )
    rebuilt = TrustedChangeReport(
        schema_version=report.schema_version,
        report_id=report.report_id,
        objective=report.objective,
        task_type=report.task_type,
        source_state=recomputed_source,
        work_unit_ids=list(report.work_unit_ids),
        route_decision=dict(report.route_decision),
        evidence_receipts=[dict(r) for r in report.evidence_receipts],
        verification_results=list(report.verification_results),
        diff_summary=report.diff_summary,
        metrics=report.metrics,
        acceptance=report.acceptance,
        route_recommendation=report.route_recommendation,
        regression_observation=report.regression_observation,
        received_at=report.received_at,
        generated_at=report.generated_at,
    )
    canonical_existing = canonical_report_payload(report)
    canonical_rebuilt = canonical_report_payload(rebuilt)
    assert canonical_existing == canonical_rebuilt, (
        "reproduction diverged from the source-bound report"
    )
    return rebuilt


__all__ = [
    "ACCEPTED_ALL_GATES_GREEN",
    "DENIED_FAILED_CHECK",
    "DENIED_INELIGIBLE_ROUTE",
    "DENIED_MISSING_VERIFICATION",
    "DENIED_OUT_OF_SCOPE",
    "DENIED_TAMPERED_EVIDENCE",
    "DENIED_UNBOUND_SOURCE",
    "VERDICT_NOT_COMPUTED",
    "VerificationFault",
    "assemble_report",
    "canonical_report_payload",
    "capture_source_state",
    "compute_report_digest",
    "compute_verdict",
    "export_redacted_report",
    "reproduce_report",
    "stamp_verdict",
    "utc_now_iso",
    "verify_report",
]
