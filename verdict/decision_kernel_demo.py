"""Credential-free demo fixtures for the DecisionKernel facade (VER-002 #219).

Every function here builds in-memory inputs and calls :func:`verdict.decision_kernel.decide`
over them. There is no network, no provider credential, no wall-clock reliance
(FR-009, NFR-001). The fixtures double as the acceptance-test seed source.

The quickstart (``specs/003-deterministic-decision-kernel/quickstart.md``) exercises
this module end to end.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from verdict.availability import AvailabilityCandidate, AvailabilityReport, AvailabilityState
from verdict.contracts import TaskSpec
from verdict.decision_kernel import AdvisoryInput, decide
from verdict.models import ModelInfo

__all__ = [
    "build_demo_decision",
    "build_demo_decision_with_advisory_promoting_excluded",
    "degraded_decision",
    "degraded_inputs",
    "demo_inputs",
    "denied_decision",
    "denied_inputs",
]


# ---------------------------------------------------------------------------
# Shared in-memory fixtures (no credentials, no network).
# ---------------------------------------------------------------------------


def _task_spec() -> TaskSpec:
    """A mundane, fully-specified code-review task -- eligible for ``sonnet``."""

    return TaskSpec(
        objective="Review a small diff for correctness and security",
        task_type="review",
        required_capabilities=["code-review"],
        tools=["cli"],
        privacy="standard",
        risk="low",
    )


def _catalog() -> list[ModelInfo]:
    """Two capable candidates; ``sonnet`` is the preferred (first) one."""

    return [
        ModelInfo(
            id="omniroute/sonnet",
            provider="omniroute",
            capability_tier=3,
            capabilities=frozenset({"code-review"}),
        ),
        ModelInfo(
            id="omniroute/haiku",
            provider="omniroute",
            capability_tier=2,
            capabilities=frozenset({"code-review"}),
        ),
    ]


def _healthy_report(models: list[ModelInfo]) -> AvailabilityReport:
    """A freshly-healthy availability report admitting every listed model."""

    cands = tuple(
        AvailabilityCandidate(model=m, state=AvailabilityState.READY, source="fixture")
        for m in models
    )
    return AvailabilityReport(
        candidates=cands, eligible=cands, source="fixture", freshness_seconds=0.0
    )


def _down_report(model: ModelInfo) -> AvailabilityReport:
    """A report marking ``model`` as UNAVAILABLE (fail-closed under protected work)."""

    cands = (
        AvailabilityCandidate(model=model, state=AvailabilityState.UNAVAILABLE, source="fixture"),
    )
    return AvailabilityReport(
        candidates=cands, eligible=(), source="fixture", freshness_seconds=0.0
    )


def _accepted_truth() -> dict[str, AvailabilityReport]:
    cats = _catalog()
    return {m.id: _healthy_report([m]) for m in cats}


# ---------------------------------------------------------------------------
# P1: the accepted fixture + its re-verify inputs.
# ---------------------------------------------------------------------------


def build_demo_decision() -> Any:
    """An accepted decision over a fully-eligible, healthy candidate catalog.

    Protected is False (the task is not flagged destructive/production-impact),
    so the healthy truth admits both candidates; ``sonnet`` is preferred.
    """

    return decide(
        task_spec=_task_spec(),
        policy_version="1",
        candidates=_catalog(),
        availability_truth=_accepted_truth(),
        protected=False,
    )


def demo_inputs() -> dict[str, Any]:
    """The exact kwargs needed to re-verify :func:`build_demo_decision` from inputs.

    Independent reproduction (P4): the auditor supplies only these inputs -- no
    producer runtime -- and :func:`verify_decision` must return ``None``.
    """

    return {
        "task_spec": _task_spec(),
        "policy_version": "1",
        "candidates": _catalog(),
        "availability_truth": _accepted_truth(),
        "protected": False,
    }


def denied_inputs() -> dict[str, Any]:
    """The exact kwargs that reproduce :func:`denied_decision` from inputs (P4)."""

    cats = _catalog()
    return {
        "task_spec": TaskSpec(
            objective="Audit a ledger against a capability no fixture exposes",
            task_type="audit",
            required_capabilities=["quantum-attestation"],  # nobody has this
            privacy="standard",
            risk="low",
        ),
        "policy_version": "1",
        "candidates": cats,
        "availability_truth": {m.id: _healthy_report([m]) for m in cats},
        "protected": False,
    }


def degraded_inputs() -> dict[str, Any]:
    """The exact kwargs that reproduce :func:`degraded_decision` from inputs (P4)."""

    cats = _catalog()
    sonnet, haiku = cats
    truth = {
        sonnet.id: _down_report(sonnet),  # preferred down -> fail-closed
        haiku.id: _healthy_report([haiku]),  # survivor
    }
    return {
        "task_spec": TaskSpec(
            objective="Review a protected diff; preferred route must be available",
            task_type="review",
            required_capabilities=["code-review"],
            privacy="standard",
            risk="high",  # high risk -> protected treatment
            production_impact=True,
        ),
        "policy_version": "1",
        "candidates": cats,
        "availability_truth": truth,
        "protected": True,
    }


# ---------------------------------------------------------------------------
# P3: denied + degraded legs.
# ---------------------------------------------------------------------------


def denied_decision() -> Any:
    """A denied decision: no candidate can satisfy the required capabilities.

    The task asks for a capability no catalog member has, so the hard gate
    admit set is empty and the outcome is ``denied``.
    """

    impossible_task = TaskSpec(
        objective="Audit a ledger against a capability no fixture exposes",
        task_type="audit",
        required_capabilities=["quantum-attestation"],  # nobody has this
        privacy="standard",
        risk="low",
    )
    return decide(
        task_spec=impossible_task,
        policy_version="1",
        candidates=_catalog(),
        availability_truth={m.id: _healthy_report([m]) for m in _catalog()},
        protected=False,
    )


def degraded_decision() -> Any:
    """A degraded decision: the preferred ``sonnet`` is UNAVAILABLE but ``haiku`` is up.

    Protected is True (simulating a protected task), so the unavailable preferred
    candidate fails closed and is excluded; ``haiku`` survives -> ``degraded``.
    """

    cats = _catalog()
    sonnet, haiku = cats
    truth = {
        sonnet.id: _down_report(sonnet),  # preferred down -> fail-closed
        haiku.id: _healthy_report([haiku]),  # survivor
    }
    protected_task = TaskSpec(
        objective="Review a protected diff; preferred route must be available",
        task_type="review",
        required_capabilities=["code-review"],
        privacy="standard",
        risk="high",  # high risk -> protected treatment
        production_impact=True,
    )
    return decide(
        task_spec=protected_task,
        policy_version="1",
        candidates=cats,
        availability_truth=truth,
        protected=True,
    )


# ---------------------------------------------------------------------------
# P2: advisory attempting to promote an excluded candidate must fail.
# ---------------------------------------------------------------------------


def build_demo_decision_with_advisory_promoting_excluded() -> Any:
    """Same accepted fixture but with an advisory that tries to promote HAiku-only.

    The advisory input asks the ranker to reorder; for the SC-002 / FR-005
    test it must NOT change the admitted membership or the exclusion list vs the
    advisory-off baseline (:func:`build_demo_decision`), differing only in
    ``advisory_influence``/``advisory_consulted``.

    Here both candidates are eligible+healthy so the advisory merely reorders;
    membership stays identical to the baseline. The P2 test compares this record
    to the baseline to prove the advisory cannot weaken hard policy.
    """

    return decide(
        task_spec=_task_spec(),
        policy_version="1",
        candidates=_catalog(),
        availability_truth=_accepted_truth(),
        protected=False,
        advisory=AdvisoryInput(
            ranker=None, requested_tier=2, escalation=(None, None), label="demo-ranker"
        ),
    )


def _inputs_repr(inputs: Mapping[str, Any]) -> Mapping[str, Any]:  # pragma: no cover
    """Helper exposing the re-verify inputs for tests/debugging."""

    return dict(inputs)
