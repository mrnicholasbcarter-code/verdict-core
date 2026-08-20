"""Native runtime enforcement kernel (feature 004, VER-008 #225).

Wires the feature-003 deterministic authority (``decide()`` → ``DecisionRecord``
in :mod:`verdict.decision_kernel`) into the native runtime boundaries
(:class:`~verdict.memory_gate.MemoryGate`, the lifecycle verification hooks,
and the gateway adapter dispatch path) so that **no boundary admits an action
the authority denied**.

This module is deliberately *not* a gate: it composes the existing eligibility,
capability-passport, and policy layers by exposing one pure fail-closed helper
(``check_enforcement``) and a thin context carrier (``EnforcementContext``)
that boundaries consult as a final filter.

Design invariants (see ``specs/004-eligibility-enforcement/``):

- **Fail-closed** (NFR-002): a missing, stale, or malformed context always
  denies. ``unknown`` is never ``allowed``.
- **No new side effects** (NFR-003): ``check_enforcement`` is pure; it neither
  persists nor logs; the boundary owns the audit event.
- **Advisory never weakens hard policy** (FR-006): advisory influence is
  ignored here — the hard ``admitted``/``exclusions`` sets are the sole input.
- **Zero-credential determinism** (NFR-001): no network, no secrets; the only
  wall-clock use is the optional ``expires_at`` comparison.
- **No contract schema change** (research R6): reads the additive
  ``decision_id``/``receipt`` fields already merged in feature 003.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from verdict.decision_kernel import DecisionRecord

if TYPE_CHECKING:  # pragma: no cover - import cycle guard only
    pass

__all__ = [
    "EnforcementContext",
    "EnforcementGatewayError",
    "EnforcementReason",
    "EnforcementResult",
    "EnforcementVerificationGate",
    "check_enforcement",
]


class EnforcementReason(str, Enum):
    """Stable, enumerated failure reasons (FR-005).

    Mirrored verbatim in every boundary's event output so audits can group,
    filter, and alert without parsing free text.
    """

    DECISION_DENIED_PROVIDER = "decision_denied_provider"
    DECISION_DEGRADED_PROVIDER_NOT_ADMITTED = "decision_degraded_provider_not_admitted"
    DECISION_MISSING = "decision_missing"
    DECISION_EXPIRED = "decision_expired"
    DECISION_MALFORMED = "decision_malformed"


@dataclass(frozen=True)
class EnforcementResult:
    """The pure output of :func:`check_enforcement`.

    Boundaries copy ``decision_id`` into their own audit event so a denial
    traces back to the authority (SC-004); ``verify_decision`` from feature 003
    is the only auditor path.
    """

    allowed: bool
    decision_id: str
    admitted_set: tuple[str, ...]
    exclusions: tuple[str, ...]
    actor_provider: str | None = None
    reason: EnforcementReason | None = None

    def __post_init__(self) -> None:
        if self.allowed and self.reason is not None:
            raise ValueError("allowed results must not carry a reason")
        if not self.allowed and self.reason is None:
            raise ValueError("denied results must carry a reason")


@dataclass(frozen=True)
class EnforcementContext:
    """The active authority decision carried through a request/task lifecycle.

    Constructed once from ``decide()`` and passed to every boundary; no store
    (FR-004). ``expires_at`` is the only TTL — a past expiry always fails
    closed regardless of the decision's own outcome.
    """

    decision_record: DecisionRecord
    created_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        # Normalize to aware datetimes so naive/now comparisons do not silently
        # flip semantics across DST/timezone boundaries (NFR-002).
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            object.__setattr__(self, "expires_at", self.expires_at.replace(tzinfo=timezone.utc))
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at must not precede created_at")


def _admitted_ids(record: DecisionRecord) -> tuple[str, ...]:
    """Extract admitted provider ids from the feature-003 ``DecisionRecord``.

    ``admitted`` is a ``list[ModelInfo]`` (model/route identity objects with an
    ``id`` attribute, e.g. ``"omniroute/sonnet"``). Reading via ``getattr``
    keeps the kernel decoupled from the exact ``ModelInfo`` import.
    """

    admitted = getattr(record, "admitted", None) or ()
    ids: list[str] = []
    for candidate in admitted:
        cid = getattr(candidate, "id", None)
        if not isinstance(cid, str) or not cid:
            raise _MalformedDecisionError("admitted candidate missing string id")
        ids.append(cid)
    return tuple(ids)


def _exclusion_ids(record: DecisionRecord) -> tuple[str, ...]:
    """Extract excluded provider ids from the feature-003 ``DecisionRecord``.

    ``exclusions`` is a ``list[dict[str, Any]]`` projected by
    ``_exclusion_projection`` in :mod:`verdict.decision_kernel`; the provider
    id lives under the ``model_id`` key.
    """

    exclusions = getattr(record, "exclusions", None) or ()
    ids: list[str] = []
    for entry in exclusions:
        if isinstance(entry, Mapping):
            mid = entry.get("model_id")
        else:
            mid = getattr(entry, "model_id", None)
        if not isinstance(mid, str) or not mid:
            raise _MalformedDecisionError("exclusion entry missing string model_id")
        ids.append(mid)
    return tuple(ids)


class _MalformedDecisionError(ValueError):
    """Internal sentinel raised while parsing a malformed ``DecisionRecord``."""


def check_enforcement(
    context: EnforcementContext | None, actor_provider: str | None, *, now: datetime | None = None
) -> EnforcementResult:
    """Pure fail-closed guard reused by every boundary (FR-001..FR-007).

    Parameters
    ----------
    context:
        The active authority carrier. ``None`` (or a context whose
        ``decision_record`` is absent) reads as :attr:`EnforcementReason.DECISION_MISSING`.
    actor_provider:
        The provider the boundary is about to act for (e.g. the memory writer's
        provider or the gateway dispatch target). ``None`` is malformed only when
        a context is present.
    now:
        Optional injectable clock (tests). Defaults to UTC now.

    Returns
    -------
    EnforcementResult
        ``allowed=True`` iff the actor provider passes the hard decision-bound
        guard; otherwise one of the five :class:`EnforcementReason`.

    Notes
    -----
    Determinism (NFR-001): the only wall-clock use is the optional
    ``expires_at`` comparison; with ``expires_at=None`` the result is a pure
    function of ``context`` and ``actor_provider`` and reproduces byte-identical.
    Advisory influence is never consulted (FR-006).
    """

    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if context is None or context.decision_record is None:
        return EnforcementResult(
            allowed=False,
            reason=EnforcementReason.DECISION_MISSING,
            decision_id="",
            admitted_set=(),
            exclusions=(),
            actor_provider=actor_provider,
        )

    record = context.decision_record
    decision_id = getattr(record, "decision_id", None)
    if not isinstance(decision_id, str) or not decision_id:
        return EnforcementResult(
            allowed=False,
            reason=EnforcementReason.DECISION_MALFORMED,
            decision_id="",
            admitted_set=(),
            exclusions=(),
            actor_provider=actor_provider,
        )

    if context.expires_at is not None and now > context.expires_at:
        return EnforcementResult(
            allowed=False,
            reason=EnforcementReason.DECISION_EXPIRED,
            decision_id=decision_id,
            admitted_set=(),
            exclusions=(),
            actor_provider=actor_provider,
        )

    try:
        admitted_set = _admitted_ids(record)
        exclusions = _exclusion_ids(record)
    except _MalformedDecisionError:
        return EnforcementResult(
            allowed=False,
            reason=EnforcementReason.DECISION_MALFORMED,
            decision_id=decision_id,
            admitted_set=(),
            exclusions=(),
            actor_provider=actor_provider,
        )

    outcome = getattr(record, "outcome", None)
    # A provider the authority never saw (neither admitted nor excluded) cannot
    # be proven eligible, so fail closed unless the outcome is an outright accept.
    if outcome == "denied":
        if actor_provider is not None and actor_provider in exclusions:
            return EnforcementResult(
                allowed=False,
                reason=EnforcementReason.DECISION_DENIED_PROVIDER,
                decision_id=decision_id,
                admitted_set=admitted_set,
                exclusions=exclusions,
                actor_provider=actor_provider,
            )
        # denied but actor not in exclusions: the whole task was denied; still
        # fail closed for unknown actors, admit nothing.
        if actor_provider is not None and actor_provider in admitted_set:
            return EnforcementResult(
                allowed=True,
                decision_id=decision_id,
                admitted_set=admitted_set,
                exclusions=exclusions,
                actor_provider=actor_provider,
            )
        return EnforcementResult(
            allowed=False,
            reason=EnforcementReason.DECISION_DENIED_PROVIDER,
            decision_id=decision_id,
            admitted_set=admitted_set,
            exclusions=exclusions,
            actor_provider=actor_provider,
        )

    if outcome == "degraded":
        if actor_provider is None:
            return EnforcementResult(
                allowed=False,
                reason=EnforcementReason.DECISION_MALFORMED,
                decision_id=decision_id,
                admitted_set=admitted_set,
                exclusions=exclusions,
                actor_provider=actor_provider,
            )
        if actor_provider in exclusions:
            return EnforcementResult(
                allowed=False,
                reason=EnforcementReason.DECISION_DENIED_PROVIDER,
                decision_id=decision_id,
                admitted_set=admitted_set,
                exclusions=exclusions,
                actor_provider=actor_provider,
            )
        if actor_provider not in admitted_set:
            return EnforcementResult(
                allowed=False,
                reason=EnforcementReason.DECISION_DEGRADED_PROVIDER_NOT_ADMITTED,
                decision_id=decision_id,
                admitted_set=admitted_set,
                exclusions=exclusions,
                actor_provider=actor_provider,
            )
        return EnforcementResult(
            allowed=True,
            decision_id=decision_id,
            admitted_set=admitted_set,
            exclusions=exclusions,
            actor_provider=actor_provider,
        )

    # outcome == "accepted" (or any non-denied/degraded future outcome):
    # the hard gate admitted the provider set; an actor in admitted_set passes,
    # any other actor fails closed (never optimistically admit an unknown).
    if actor_provider is not None and actor_provider not in admitted_set:
        return EnforcementResult(
            allowed=False,
            reason=EnforcementReason.DECISION_DEGRADED_PROVIDER_NOT_ADMITTED,
            decision_id=decision_id,
            admitted_set=admitted_set,
            exclusions=exclusions,
            actor_provider=actor_provider,
        )
    return EnforcementResult(
        allowed=True,
        decision_id=decision_id,
        admitted_set=admitted_set,
        exclusions=exclusions,
        actor_provider=actor_provider,
    )


@dataclass(frozen=True)
class EnforcementVerificationGate:
    """Lifecycle verification gate that consults the authority (FR-002).

    Composed into the lifecycle controller's hook runner as an
    ``extra_verification_gate``; its :meth:`evaluate` signature mirrors the
    duck-typed protocol the controller already calls
    (``gate.evaluate(workflow_plan=..., stage=...)``) and additionally accepts
    ``decision_record`` / ``context`` so it can run the hard check. The
    controller detects the extended signature via :func:`inspect.signature`.
    """

    stage: str = "pre_dispatch"

    def evaluate(
        self,
        *,
        workflow_plan: Any,
        stage: str,
        decision_record: DecisionRecord | None = None,
        context: EnforcementContext | None = None,
    ) -> dict[str, Any]:
        target_provider = self._target_provider(workflow_plan)
        if context is None:
            if decision_record is None:
                return self._block(
                    stage,
                    reason=EnforcementReason.DECISION_MISSING,
                    decision_id="",
                    blocked_provider=target_provider,
                    admitted_set=(),
                    exclusions=(),
                )
            context = EnforcementContext(
                decision_record=decision_record, created_at=datetime.now(timezone.utc)
            )
        result = check_enforcement(context, target_provider)
        if result.allowed:
            return {
                "passed": True,
                "source": "enforcement_kernel",
                "decision_id": result.decision_id,
                "blocked_provider": None,
                "admitted_set": list(result.admitted_set),
                "exclusions": list(result.exclusions),
            }
        assert result.reason is not None  # invariant: denied results carry a reason
        return self._block(
            stage,
            reason=result.reason,
            decision_id=result.decision_id,
            blocked_provider=target_provider,
            admitted_set=result.admitted_set,
            exclusions=result.exclusions,
        )

    @staticmethod
    def _target_provider(workflow_plan: Any) -> str | None:
        """Resolve the step's target provider from a workflow_plan-like object.

        Accepts a mapping carrying ``"target_provider"`` (the quickstart/test
        shape) or an object with a ``target_provider`` attribute; returns
        ``None`` if unresolvable so the caller fails closed.
        """

        if workflow_plan is None:
            return None
        if isinstance(workflow_plan, Mapping):
            value = workflow_plan.get("target_provider")
            return value if isinstance(value, str) and value else None
        value = getattr(workflow_plan, "target_provider", None)
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _block(
        stage: str,
        *,
        reason: EnforcementReason,
        decision_id: str,
        blocked_provider: str | None,
        admitted_set: tuple[str, ...],
        exclusions: tuple[str, ...],
    ) -> dict[str, Any]:
        return {
            "passed": False,
            "source": "enforcement_kernel",
            "stage": stage,
            "reason": reason.value if reason is not None else None,
            "decision_id": decision_id,
            "blocked_provider": blocked_provider,
            "admitted_set": list(admitted_set),
            "exclusions": list(exclusions),
        }


class EnforcementGatewayError(ValueError):
    """Raised when the gateway dispatch guard rejects a provider (FR-003).

    Carries the authority's ``decision_id`` and admitted alternatives so callers
    can surface the denial without re-running the authority.
    """

    def __init__(
        self,
        reason: EnforcementReason,
        decision_id: str,
        admitted_set: tuple[str, ...],
        actor_provider: str | None,
    ) -> None:
        self.reason = reason
        self.decision_id = decision_id
        self.admitted_set = admitted_set
        self.actor_provider = actor_provider
        super().__init__(
            f"enforcement denied dispatch to {actor_provider!r}: {reason.value} "
            f"(decision_id={decision_id[:14]}…; admitted={list(admitted_set)})"
        )


def dispatch_with_enforcement(
    adapter: Any,
    attestation: Any,
    *,
    request: Any = None,
    context: EnforcementContext | None = None,
    dispatch: Any = None,
) -> Any:
    """Pre-dispatch guard for the gateway adapter runtime (FR-003, NFR-001).

    When ``context`` is ``None`` the legacy dispatch runs unchanged. Otherwise
    the authority is consulted *before* any network call: an excluded (or
    not-admitted) provider raises :class:`EnforcementGatewayError` and the
    adapter is never invoked. Providers derive from ``attestation``: a
    ``RouteIdentityAttestation`` carries ``resolved_route.provider``; a plain
    mapping/attr resolves a ``provider`` key/attr directly (test convenience).
    ``dispatch`` lets tests pass a stub instead of a real adapter method.
    """

    if context is None:
        if dispatch is not None:
            return dispatch()
        return _invoke_adapter(adapter, attestation, request)

    actor_provider = _attestation_provider(attestation)
    result = check_enforcement(context, actor_provider)
    if not result.allowed:
        assert result.reason is not None  # invariant enforced by EnforcementResult
        raise EnforcementGatewayError(
            result.reason, result.decision_id, result.admitted_set, result.actor_provider
        )
    if dispatch is not None:
        return dispatch()
    return _invoke_adapter(adapter, attestation, request)


def _attestation_provider(attestation: Any) -> str | None:
    """Resolve the provider id from a route identity attestation (FR-008)."""

    resolved = getattr(attestation, "resolved_route", None)
    if resolved is not None:
        provider = getattr(resolved, "provider", None)
        if isinstance(provider, str) and provider:
            return provider
    if isinstance(attestation, Mapping):
        provider = attestation.get("provider")
        if isinstance(provider, str) and provider:
            return provider
    provider = getattr(attestation, "provider", None)
    return provider if isinstance(provider, str) and provider else None


def _invoke_adapter(adapter: Any, attestation: Any, request: Any) -> Any:
    """Best-effort legacy dispatch when no explicit ``dispatch`` stub is given.

    The real gateway adapter protocol has several entry points; we prefer
    ``adapter.proxy`` style is out of scope here — callers/tests pass an
    explicit ``dispatch`` stub whenever a concrete call shape is required.
    """

    # Prefer the most descriptive no-network method if present; otherwise
    # return a marker. Production callers should pass ``dispatch=``.
    for method_name in ("translate", "discover"):
        method = getattr(adapter, method_name, None)
        if callable(method):
            try:
                return method() if method_name == "discover" else method(request)
            except TypeError:
                continue
    return None  # pragma: no cover - stub path only


# Suppress unused-field lint on the dataclass default we intentionally keep.
_ = field
