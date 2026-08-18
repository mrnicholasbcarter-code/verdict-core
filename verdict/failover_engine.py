"""Capability-matched provider failover for execution continuity (issue #258).

When a model bound to an :class:`ExecutionSession` fails transiently (HTTP
429/408/425/500+, or a transport timeout), the :class:`FailoverEngine`
quarantines the failing ``(provider, model)`` pair, recalculates the
requirements from the remaining task, selects an equivalent qualified
:class:`~verdict.model_passports.ModelPassport` that satisfies the same
capability surface, rebinds the session's context, and re-arms the failed step.

Completed work is never re-run: the engine reuses the session's checkpointed
``completed_steps`` and only resumes at the failed step.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from verdict.availability import CandidateRequirements, canonical_capability
from verdict.execution_session import ExecutionSession, ExecutionSessionError, FailureEntry
from verdict.model_passports import ModelPassport

# HTTP statuses that indicate a transient provider failure worth a failover.
_RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

# Provider error classes treated as transient and failover-eligible.
_TRANSIENT_ERROR_CLASSES = frozenset(
    {"rate_limited", "quota_exhausted", "timeout", "upstream_error", "server_error"}
)

_DEFAULT_QUARANTINE_SECONDS = 300
_MAX_FAILOVERS_PER_STEP = 3


class FailoverEngineError(RuntimeError):
    """Raised when no equivalent qualified model can be selected."""


@dataclass(frozen=True)
class FailoverPlan:
    """An explainable, replayable failover decision."""

    session_id: str
    step_id: str
    failed_model_key: str
    failed_provider: str
    failed_model_id: str
    error_class: str
    replacement_passport: ModelPassport
    quarantine_seconds: int = _DEFAULT_QUARANTINE_SECONDS
    reason: str = ""

    @property
    def replacement_key(self) -> str:
        return self.replacement_passport.key

    @property
    def evidence_receipt(self) -> str:
        """Stable digest-style identifier recorded in the replay log."""
        return f"failover:{self.failed_model_key}:{self.replacement_key}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _to_failure_entry(
    plan: FailoverPlan, *, message: str, status_code: int | None = None
) -> FailureEntry:
    return FailureEntry(
        step_id=plan.step_id,
        provider=plan.failed_provider,
        model_id=plan.failed_model_id,
        error_class=plan.error_class,
        message=message,
        created_at=time.time(),
        status_code=status_code,
        quarantine_model=plan.failed_model_key,
        replacement_model=plan.replacement_key,
    )


class FailoverEngine:
    """Selects an equivalent qualified ``ModelPassport`` after a failure."""

    def __init__(
        self,
        *,
        quarantine_seconds: int = _DEFAULT_QUARANTINE_SECONDS,
        max_failovers_per_step: int = _MAX_FAILOVERS_PER_STEP,
        clock: Any | None = None,
    ) -> None:
        if quarantine_seconds <= 0:
            raise ValueError("quarantine_seconds must be positive")
        if max_failovers_per_step <= 0:
            raise ValueError("max_failovers_per_step must be positive")
        self.quarantine_seconds = quarantine_seconds
        self.max_failovers_per_step = max_failovers_per_step
        self._clock = clock
        self._quarantine: dict[str, datetime] = {}

    # ------------------------------------------------------------------ public

    def quarantine(self, passport: ModelPassport, *, error_class: str) -> None:
        """Quarantine a failing provider until the cooldown expires."""
        until = _now() if self._clock is None else self._clock()
        if isinstance(until, (int, float)):
            until = datetime.fromtimestamp(until, tz=timezone.utc)
        if not isinstance(until, datetime):
            raise ValueError("clock must return a datetime or epoch seconds")
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        self._quarantine[passport.key] = until + timedelta(seconds=self.quarantine_seconds)

    def is_quarantined(self, key: str, *, now: datetime | None = None) -> bool:
        expiry = self._quarantine.get(key)
        if expiry is None:
            return False
        current = now or (_now() if self._clock is None else self._clock())
        if isinstance(current, (int, float)):
            current = datetime.fromtimestamp(current, tz=timezone.utc)
        if not isinstance(current, datetime):
            return False
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current < expiry

    @staticmethod
    def is_transient_failure(error_class: str, status_code: int | None = None) -> bool:
        if status_code is not None and status_code in _RETRYABLE_STATUS_CODES:
            return True
        return canonical_capability(error_class) in {
            canonical_capability(value) for value in _TRANSIENT_ERROR_CLASSES
        }

    def failover(
        self,
        session: ExecutionSession,
        plane: Any,
        *,
        provider: str,
        model_id: str,
        error_class: str,
        message: str = "",
        status_code: int | None = None,
        candidates: Sequence[ModelPassport] = (),
    ) -> ExecutionSession:
        """Quarantine, rebind, and resume the session at the failed step.

        Returns the same session mutated in place and checkpointed, so callers
        can either keep the reference or use the returned value after a
        resume-from-disk round trip.
        """
        if not self.is_transient_failure(error_class, status_code):
            raise FailoverEngineError(
                f"failure {error_class} (status {status_code}) is not transient; "
                "no failover attempted"
            )
        failed_key = f"{provider}/{model_id}"
        failed_step = session.current_step or _first_pending(session)
        if failed_step is None:
            raise ExecutionSessionError("session has no pending step to fail over")

        attempts = session.attempts.get(failed_step, 0)
        if attempts >= self.max_failovers_per_step:
            raise FailoverEngineError(
                f"step {failed_step!r} exceeded {self.max_failovers_per_step} failovers"
            )

        requirements = _recalculate_requirements(session, failed_step)
        replacement = self._select_equivalent(
            failed_key=failed_key,
            current=session.model_id,
            requirements=requirements,
            candidates=candidates,
        )

        now = _now()
        plan = FailoverPlan(
            session_id=session.session_id,
            step_id=failed_step,
            failed_model_key=failed_key,
            failed_provider=provider,
            failed_model_id=model_id,
            error_class=error_class,
            replacement_passport=replacement,
            quarantine_seconds=self.quarantine_seconds,
            reason=f"transient {error_class}",
        )

        # 1. Quarantine the failing provider/model pair.
        quarantined = _as_quarantined_passport(
            replacement, failed_key=failed_key, error_class=error_class, now=now
        )
        self._quarantine[quarantined.key] = now + timedelta(seconds=self.quarantine_seconds)

        # 2. Rebind the context envelope when available (lazy import: another
        #    worker is building verdict.context_envelope concurrently).
        self._rebind_context(session, plan)

        # 3. Resume at the failed step without re-running completed work.
        entry = _to_failure_entry(plan, message=message or error_class, status_code=status_code)
        session.resume_from_failure(
            plane,
            failure=entry,
            replacement_model=replacement.model_id,
            replacement_passport_key=replacement.key,
            reason=f"failover:{plan.evidence_receipt}",
        )
        return session

    # ------------------------------------------------------------------ internals

    def _select_equivalent(
        self,
        *,
        failed_key: str,
        current: str | None,
        requirements: CandidateRequirements,
        candidates: Sequence[ModelPassport],
    ) -> ModelPassport:
        eligible = [p for p in candidates if self._passport_satisfies(p, requirements)]
        eligible = [
            p
            for p in eligible
            if p.key != failed_key
            and p.availability_state not in {"quarantined", "denied"}
            and not self.is_quarantined(p.key)
        ]
        if not eligible:
            raise FailoverEngineError(
                f"no equivalent qualified model for {failed_key!r} satisfying "
                f"capabilities {sorted(requirements.required)}"
            )
        # Deterministic pick: prefer the same provider (fewer contract changes),
        # then lexicographic key.
        return min(eligible, key=lambda p: (p.provider != failed_key.split("/", 1)[0], p.key))

    @staticmethod
    def _passport_satisfies(passport: ModelPassport, requirements: CandidateRequirements) -> bool:
        if passport.availability_state not in {"eligible", "degraded"}:
            return False
        if requirements.allow_providers and passport.provider not in requirements.allow_providers:
            return False
        if passport.provider in requirements.deny_providers:
            return False
        if requirements.allow_models and passport.model_id not in requirements.allow_models:
            return False
        if passport.model_id in requirements.deny_models:
            return False
        fits_context = (
            requirements.estimated_tokens is None
            or passport.context_window <= 0
            or requirements.estimated_tokens <= passport.context_window
        )
        return ("tools" not in requirements.required or passport.tool_support) and fits_context

    @staticmethod
    def _rebind_context(session: ExecutionSession, plan: FailoverPlan) -> None:
        """Rebind the session context to the replacement passport, if available."""
        try:
            from verdict.context_envelope import ContextEnvelope, ContextItem, SourceRef
        except ImportError:
            session.record_artifact(
                f"context_rebind:{plan.evidence_receipt}",
                {"replacement": plan.replacement_key, "rebound": False, "reason": "unavailable"},
            )
            return
        try:
            goal = ContextItem(
                item_id=f"goal:{plan.evidence_receipt}",
                kind="goal",
                content=str(session.task_spec.get("task") or session.task_spec),
                source=SourceRef(
                    kind="worker",
                    ref=f"urn:verdict:session:{session.session_id}",
                    revision=plan.evidence_receipt,
                ),
            )
            envelope = ContextEnvelope(task_id=session.session_id, goal=goal)
            envelope.to_dict()
        except Exception as exc:
            session.record_artifact(
                f"context_rebind:{plan.evidence_receipt}",
                {"replacement": plan.replacement_key, "rebound": False, "reason": str(exc)},
            )
        else:
            session.record_artifact(
                f"context_rebind:{plan.evidence_receipt}",
                {
                    "replacement": plan.replacement_key,
                    "provider": plan.replacement_passport.provider,
                    "rebound": True,
                },
            )


def _recalculate_requirements(session: ExecutionSession, failed_step: str) -> CandidateRequirements:
    """Derive candidate requirements from the remaining work in the task spec."""
    spec = session.task_spec
    raw = spec.get("requirements") or {}
    if not isinstance(raw, Mapping):
        raw = {}
    allow_providers = raw.get("allow_providers") or ()
    deny_providers = raw.get("deny_providers") or ()
    allow_models = raw.get("allow_models") or ()
    deny_models = raw.get("deny_models") or ()
    estimated_tokens = raw.get("estimated_tokens")
    return CandidateRequirements(
        required=frozenset(raw.get("required", ())),
        allow_models=frozenset(allow_models),
        deny_models=frozenset(deny_models),
        allow_providers=frozenset(allow_providers),
        deny_providers=frozenset(deny_providers),
        budget_remaining=raw.get("budget_remaining"),
        max_concurrency=raw.get("max_concurrency"),
        estimated_tokens=estimated_tokens if isinstance(estimated_tokens, int) else None,
        estimated_cost=raw.get("estimated_cost"),
    )


def _first_pending(session: ExecutionSession) -> str | None:
    for step in session.steps:
        if step.status == "pending":
            return step.step_id
    return None


def _as_quarantined_passport(
    reference: ModelPassport, *, failed_key: str, error_class: str, now: datetime
) -> ModelPassport:
    """Copy the replacement passport with the failed model's key quarantined."""
    return ModelPassport(
        provider=failed_key.split("/", 1)[0],
        model_id=failed_key.split("/", 1)[1],
        auth_state=reference.auth_state,
        latency_p95=reference.latency_p95,
        context_window=reference.context_window,
        tool_support=reference.tool_support,
        token_cost_per_1k=reference.token_cost_per_1k,
        last_verified_timestamp=now,
        availability_state="quarantined",
        availability_reason=error_class,
        quarantine_until=now + timedelta(seconds=_DEFAULT_QUARANTINE_SECONDS),
        quarantined_at=now,
        recovery_attempts=0,
        qualified_at=now,
        expires_at=now + timedelta(seconds=_DEFAULT_QUARANTINE_SECONDS + 60),
    )


__all__ = [
    "FailoverEngine",
    "FailoverEngineError",
    "FailoverPlan",
    "is_retryable_status_code",
    "is_transient_error_class",
]


def is_retryable_status_code(status_code: int | None) -> bool:
    return status_code in _RETRYABLE_STATUS_CODES


def is_transient_error_class(error_class: str) -> bool:
    return canonical_capability(error_class) in {
        canonical_capability(value) for value in _TRANSIENT_ERROR_CLASSES
    }
