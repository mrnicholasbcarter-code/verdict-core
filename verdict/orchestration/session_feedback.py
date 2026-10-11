"""Project classified live-worker availability failures into the shared health cache.

SessionLedger remains the task-outcome store. This module writes only negative
availability evidence, never task quality, capability passes, or admission.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime

from verdict.orchestration.contracts import FailureClassification, WorkerTerminal
from verdict.orchestration.health_cache import (
    LONG_CAP_SECONDS,
    HealthCache,
    HealthCacheError,
    HealthEntry,
    ProbeResult,
    ScopedCooldown,
    format_datetime,
)

# Runtime classifier names differ from the at-rest probe category vocabulary.
_CACHE_CATEGORIES = {
    "rate_limited": "rate_limited",
    "quota_exhausted": "rate_limited",
    "authentication": "authentication",
    "payment_required": "payment_required",
    "permission": "permission",
    "unsupported": "not_found",
    "model_unavailable": "not_found",
    "gone": "gone",
    "bad_request": "bad_request",
    "upstream_temporary": "upstream",
    "transport_temporary": "upstream",
    "timeout": "timeout",
    "empty_output": "empty_output",
    "no_final_answer": "no_final_answer",
    "malformed_response": "malformed_response",
    "model_mismatch": "model_mismatch",
}
_CREDENTIAL_CATEGORIES = frozenset(
    {"rate_limited", "quota_exhausted", "authentication", "payment_required", "permission"}
)
_NON_AVAILABILITY_ERRORS = (
    "verification_failed",
    "ownership_violation",
    "review_failed",
    "task_feedback",
)


@dataclass(frozen=True)
class SessionFeedbackResult:
    """Write acknowledgement. A skipped observation supplies an explicit reason."""

    recorded: bool
    reason: str
    entry: HealthEntry | None = None
    cooldown_key: str | None = None


def record_worker_failure(
    cache: HealthCache,
    *,
    failure: FailureClassification,
    terminal: WorkerTerminal,
    route_id: str,
    provider_id: str,
    pool: str | None,
    now: datetime,
    turn_count: int | None = None,
    provenance: str | None = None,
    injected: bool = False,
) -> SessionFeedbackResult:
    """Durably narrow availability after an adapter-attested real failure.

    ``failure`` must come from the current terminal classifier. ``pool`` is the
    selected canonical credential identity, not a provider-prefixed alias key.
    Unknown/scripted/injected executor provenance cannot supply live evidence.
    Only availability failures with positive, finite cooldowns are recorded.
    Provider-scope credential failures block the selected pool (or the provider
    if no pool is known). Route scope never affects sibling routes or buckets.

    Turn counts are optional observations, not a health/capability threshold.
    Provenance is hashed into an opaque bounded digest; raw errors, transcripts,
    session references and credentials are never copied to the cache. Lock and
    persistence errors propagate so callers can report a failed write explicitly.
    """
    if (
        injected
        or terminal.executor_kind == "fault-injected"
        or terminal.session_ref.startswith("fault-injected")
    ):
        return SessionFeedbackResult(False, "injected_terminal")
    if terminal.executor_kind != "live":
        return SessionFeedbackResult(False, "unattested_terminal")
    if terminal.ok and (failure.category != "empty_output" or terminal.output.strip()):
        return SessionFeedbackResult(False, "successful_terminal")
    if terminal.error.startswith(_NON_AVAILABILITY_ERRORS):
        return SessionFeedbackResult(False, "non_availability_failure")
    if (
        failure.category not in _CACHE_CATEGORIES
        or failure.action != "REROUTE"
        or failure.scope not in {"route", "provider"}
        or (failure.scope == "provider" and failure.category not in _CREDENTIAL_CATEGORIES)
    ):
        return SessionFeedbackResult(False, "non_availability_failure")
    seconds = failure.cooldown_seconds
    if isinstance(seconds, bool) or not math.isfinite(seconds) or seconds <= 0:
        return SessionFeedbackResult(False, "invalid_cooldown")
    seconds = min(seconds, LONG_CAP_SECONDS)
    format_datetime(now)  # Reject naive timestamps before mutating any state.
    if not route_id.strip() or not provider_id.strip():
        raise HealthCacheError("selected route and provider must be non-empty")
    if pool is not None and not pool.strip():
        raise HealthCacheError("selected pool must be non-empty when supplied")

    observed_turns = turn_count
    turns_source = "explicit" if turn_count is not None else "unknown"
    if observed_turns is None and terminal.usage is not None:
        observed_turns = terminal.usage.turns
        turns_source = "usage.turns"
    if (
        not isinstance(observed_turns, int)
        or isinstance(observed_turns, bool)
        or observed_turns < 0
    ):
        observed_turns = None
        turns_source = "unknown"
    source = provenance if provenance is not None else terminal.session_ref
    opaque_source = (
        "sha256:" + hashlib.sha256(source.encode("utf-8")).hexdigest() if source else None
    )
    credential_failure = failure.scope == "provider"
    scope = "pool" if credential_failure and pool is not None else failure.scope
    cooldown_key = (
        (f"pool:{pool}" if pool is not None else f"provider:{provider_id.lower()}")
        if credential_failure
        else None
    )
    result = SessionFeedbackResult(False, "not_written")

    def _mutate(locked: HealthCache) -> None:
        nonlocal result
        previous = locked.entry(route_id)
        if previous is not None and (
            previous.checked_at > now or (previous.checked_at == now and not previous.healthy)
        ):
            result = SessionFeedbackResult(False, "superseded_or_duplicate")
            return
        entry = locked.record(
            route_id,
            ProbeResult(
                category=_CACHE_CATEGORIES[failure.category],
                chat_ok=False,
                tool_ok=False,
                http_status=terminal.status_code,
                pool=pool,
                failure_scope=scope,
                negative_ttl_seconds=seconds,
                feedback_source="worker_terminal",
                feedback_category=failure.category,
                feedback_turn_count=observed_turns,
                feedback_turns_source=turns_source,
                feedback_provenance=opaque_source,
            ),
            now,
        )
        if cooldown_key is not None:
            locked.record_cooldown(
                ScopedCooldown(
                    key=cooldown_key,
                    category=entry.category,
                    canonical_category=failure.category,
                    checked_at=now,
                    until=entry.until,
                    provider_id=provider_id,
                    pool=pool,
                )
            )
            if failure.category in {"rate_limited", "quota_exhausted"}:
                locked.zero_bucket(provider_id, entry.until, pool=pool)
        result = SessionFeedbackResult(True, "recorded", entry, cooldown_key)

    cache.merge_and_save(_mutate)
    # Return the revision allocated by the serialized writer, not its pre-write copy.
    if result.recorded:
        result = SessionFeedbackResult(True, "recorded", cache.entry(route_id), cooldown_key)
    return result
