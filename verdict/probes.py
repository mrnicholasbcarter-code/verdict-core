"""Bounded, privacy-safe model availability probes.

This module deliberately treats catalog identifiers as opaque runtime data.  A
catalog entry is not proof that a model can be used: callers can run a minimal
probe and feed the resulting structured observation into the availability
adapter.  No response body, prompt data, credentials, or connection strings are
stored in probe state.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event
from typing import Any, Protocol

PROBE_PROMPT = "Return exactly: OK"
PROBE_DIAGNOSTICS_VERSION = "1"


class ProbeConsentRequiredError(ValueError):
    """Raised before transport invocation when live consent is absent."""


class ProbeResponseTooLargeError(ValueError):
    """Raised when a transport receives more response bytes than allowed."""


class ProbeTransport(Protocol):
    """Injected OpenAI-compatible transport used by :class:`ProbeRunner`."""

    def __call__(
        self, model_id: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> Any: ...


@dataclass(frozen=True)
class ProbePolicy:
    """Safety and scheduling bounds for a probe run."""

    timeout_seconds: float = 20.0
    max_concurrency: int = 4
    max_models_per_run: int = 16
    cooldown_seconds: float = 30.0
    max_cooldown_seconds: float = 900.0
    failure_threshold: int = 3
    quarantine_seconds: float = 1800.0
    max_response_bytes: int = 1_048_576
    max_duration_seconds: float = 120.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        if self.max_models_per_run < 1:
            raise ValueError("max_models_per_run must be at least 1")
        if self.cooldown_seconds < 0 or self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError("cooldown bounds are invalid")
        if self.failure_threshold < 1 or self.quarantine_seconds < 0:
            raise ValueError("failure/quarantine bounds are invalid")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if self.max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be positive")

    def payload(self, model_id: str) -> dict[str, Any]:
        """Return the fixed no-user-data, one-token probe request."""
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty runtime string")
        return {
            "model": model_id,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": False,
            "tools": [],
        }


@dataclass(frozen=True)
class ProbeBudget:
    """Per-provider budget for one qualification run."""

    provider: str
    max_requests: int = 16
    max_tokens: int = 16
    max_response_bytes: int = 1_048_576
    max_duration_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty string")
        if self.max_requests < 1 or self.max_tokens < 1:
            raise ValueError("request and token budgets must be positive")
        if self.max_response_bytes < 1:
            raise ValueError("response-byte budget must be positive")
        if self.max_duration_seconds <= 0:
            raise ValueError("duration budget must be positive")


@dataclass(frozen=True)
class ProbeRunDiagnostics:
    """Sanitized, versioned accounting for one probe run."""

    provider: str
    live: bool
    consented: bool
    max_requests: int
    max_tokens: int
    max_response_bytes: int
    max_duration_seconds: float
    requested_models: int
    unique_models: int
    bounded_models: int
    scheduled_models: int
    transport_calls: int
    skipped_models: int
    cancelled_models: int
    budget_limited_models: int
    observed_tokens: int
    response_bytes: int
    statuses: Mapping[str, int]
    started_at: datetime
    finished_at: datetime
    cancellation_requested: bool = False

    @property
    def duration_ms(self) -> float:
        return round((self.finished_at - self.started_at).total_seconds() * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        """Return only deterministic, non-sensitive run accounting."""

        return {
            "diagnostics_version": PROBE_DIAGNOSTICS_VERSION,
            "provider": self.provider,
            "live": self.live,
            "consented": self.consented,
            "max_requests": self.max_requests,
            "max_tokens": self.max_tokens,
            "max_response_bytes": self.max_response_bytes,
            "max_duration_seconds": self.max_duration_seconds,
            "requested_models": self.requested_models,
            "unique_models": self.unique_models,
            "bounded_models": self.bounded_models,
            "scheduled_models": self.scheduled_models,
            "transport_calls": self.transport_calls,
            "skipped_models": self.skipped_models,
            "cancelled_models": self.cancelled_models,
            "budget_limited_models": self.budget_limited_models,
            "observed_tokens": self.observed_tokens,
            "response_bytes": self.response_bytes,
            "statuses": dict(sorted(self.statuses.items())),
            "started_at": _format_datetime(self.started_at),
            "finished_at": _format_datetime(self.finished_at),
            "duration_ms": self.duration_ms,
            "cancellation_requested": self.cancellation_requested,
        }


@dataclass(frozen=True)
class ProbeRun:
    """Observations plus replayable accounting for one run."""

    observations: tuple[ProbeObservation, ...]
    diagnostics: ProbeRunDiagnostics


@dataclass(frozen=True)
class ProbeObservation:
    """Safe operational result; never contains the upstream response body."""

    model_id: str
    availability_state: str
    status: str
    observed_at: datetime
    latency_ms: float | None = None
    usage_available: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    http_status: int | None = None
    error_class: str | None = None
    error: str | None = None
    consecutive_failures: int = 0
    next_probe_at: datetime | None = None
    quarantine_until: datetime | None = None
    response_bytes: int | None = None

    def as_runtime_observation(self) -> Any:
        """Convert to the existing structured availability-observation contract."""
        from verdict.availability import RuntimeObservation

        health = "healthy" if self.availability_state == "ready" else "degraded"
        if self.status in {"failed", "timeout"}:
            health = "unhealthy"
        raw = {
            "probe_status": self.status,
            "probe_availability_state": self.availability_state,
            "probe_error_class": self.error_class,
            "probe_error": self.error,
            "usage_available": self.usage_available,
            "http_status": self.http_status,
        }
        return RuntimeObservation(
            observed_at=self.observed_at,
            ttl_seconds=60,
            source="verdict:probe",
            health=health,
            auth="authorized" if self.availability_state == "ready" else "unknown",
            eligible=True if self.availability_state == "ready" else None,
            raw=raw,
        )


@dataclass
class _ProbeState:
    consecutive_failures: int = 0
    next_probe_at: float = 0.0
    quarantine_until: float = 0.0
    last_state: str = "unknown"


class ProbeRegistry:
    """In-memory operational state for cooldown and quarantine decisions."""

    def __init__(self) -> None:
        self._states: dict[str, _ProbeState] = {}

    def state(self, model_id: str) -> _ProbeState:
        return self._states.setdefault(model_id, _ProbeState())

    def snapshot(self) -> dict[str, dict[str, float | int | str]]:
        """Return non-sensitive state suitable for metrics/debugging."""
        return {
            model_id: {
                "consecutive_failures": state.consecutive_failures,
                "next_probe_at": state.next_probe_at,
                "quarantine_until": state.quarantine_until,
                "last_state": state.last_state,
            }
            for model_id, state in self._states.items()
        }


class ProbeRunner:
    """Run bounded probes against dynamically supplied model IDs."""

    def __init__(
        self, policy: ProbePolicy | None = None, registry: ProbeRegistry | None = None
    ) -> None:
        self.policy = policy or ProbePolicy()
        self.registry = registry or ProbeRegistry()

    def run(
        self,
        model_ids: Sequence[str],
        transport: ProbeTransport,
        *,
        now: datetime | None = None,
        live: bool = False,
        consented: bool = False,
        provider: str = "unknown",
        budget: ProbeBudget | None = None,
        cancel_event: Event | None = None,
    ) -> list[ProbeObservation]:
        """Run probes and return observations for compatibility.

        Use :meth:`run_with_diagnostics` when the caller needs the sanitized
        budget and consent accounting for replay or operator diagnostics.
        """

        return list(
            self.run_with_diagnostics(
                model_ids,
                transport,
                now=now,
                live=live,
                consented=consented,
                provider=provider,
                budget=budget,
                cancel_event=cancel_event,
            ).observations
        )

    def run_with_diagnostics(
        self,
        model_ids: Sequence[str],
        transport: ProbeTransport,
        *,
        now: datetime | None = None,
        live: bool = False,
        consented: bool = False,
        provider: str = "unknown",
        budget: ProbeBudget | None = None,
        cancel_event: Event | None = None,
    ) -> ProbeRun:
        """Probe at most ``max_models_per_run`` unique IDs in input order.

        Cooldown/quarantined models produce a structured skipped result and do
        not consume a transport slot.  The transport receives the timeout and
        must enforce it at its I/O boundary; the runner also bounds the worker
        wait so a broken transport cannot block a run indefinitely.
        """
        if live and not consented:
            raise ProbeConsentRequiredError("live probes require explicit consent")
        if not isinstance(provider, str) or not provider.strip():
            raise ValueError("provider must be a non-empty string")
        if live and provider == "unknown":
            raise ValueError("live probes require an explicit provider")
        selected_budget = budget or ProbeBudget(
            provider=provider,
            max_requests=self.policy.max_models_per_run,
            max_tokens=self.policy.max_models_per_run,
            max_response_bytes=self.policy.max_response_bytes,
            max_duration_seconds=self.policy.max_duration_seconds,
        )
        if selected_budget.provider != provider:
            raise ValueError("probe budget provider does not match run provider")
        observed_at = now or datetime.now(timezone.utc)
        started_at = datetime.now(timezone.utc)
        now_mono = time.monotonic()
        requested_count = len(model_ids)
        unique: list[str] = []
        seen: set[str] = set()
        for model_id in model_ids:
            if isinstance(model_id, str) and model_id and model_id not in seen:
                unique.append(model_id)
                seen.add(model_id)
        bounded = unique[: self.policy.max_models_per_run]
        results: dict[str, ProbeObservation] = {}
        pending: list[str] = []
        for model_id in bounded:
            state = self.registry.state(model_id)
            if state.quarantine_until > now_mono:
                results[model_id] = self._skipped(
                    model_id, observed_at, now_mono, state, "quarantined"
                )
            elif state.next_probe_at > now_mono:
                results[model_id] = self._skipped(
                    model_id, observed_at, now_mono, state, "cooldown"
                )
            else:
                pending.append(model_id)

        request_limit = min(selected_budget.max_requests, selected_budget.max_tokens)
        budget_limited = pending[request_limit:]
        pending = pending[:request_limit]
        for model_id in budget_limited:
            results[model_id] = self._budget_limited(model_id, observed_at)

        if cancel_event and cancel_event.is_set():
            for model_id in pending:
                results[model_id] = self._cancelled(model_id, observed_at)
            pending = []

        transport_calls = 0
        observed_tokens = 0
        response_bytes = 0
        cancellation_requested = bool(cancel_event and cancel_event.is_set())
        scheduled_models = 0
        if pending:
            executor = ThreadPoolExecutor(
                max_workers=min(self.policy.max_concurrency, len(pending)),
                thread_name_prefix="verdict-probe",
            )
            deadline = time.monotonic() + min(
                self.policy.timeout_seconds,
                self.policy.max_duration_seconds,
                selected_budget.max_duration_seconds,
            )
            remaining_models = list(pending)
            while remaining_models:
                if cancel_event and cancel_event.is_set():
                    cancellation_requested = True
                    for model_id in remaining_models:
                        results[model_id] = self._cancelled(model_id, observed_at)
                    remaining_models = []
                    break
                if response_bytes >= selected_budget.max_response_bytes:
                    for model_id in remaining_models:
                        results[model_id] = self._budget_limited(model_id, observed_at)
                    remaining_models = []
                    break
                if observed_tokens >= selected_budget.max_tokens:
                    for model_id in remaining_models:
                        results[model_id] = self._budget_limited(model_id, observed_at)
                    remaining_models = []
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    for model_id in remaining_models:
                        results[model_id] = self._record_failure(
                            model_id, observed_at, now_mono, "timeout", "probe timed out"
                        )
                    remaining_models = []
                    break
                # Response sizes are unknown until transport returns. Run one
                # request at a time so aggregate byte accounting is exact and
                # cannot authorize another request after the budget is crossed.
                batch = remaining_models[:1]
                remaining_models = remaining_models[1:]
                futures: dict[Future[Any], str] = {
                    executor.submit(self._invoke, transport, model_id): model_id
                    for model_id in batch
                }
                scheduled_models += len(futures)
                transport_calls += len(futures)
                done, not_done = wait(futures, timeout=min(remaining, 0.05))
                while not_done:
                    if cancel_event and cancel_event.is_set():
                        cancellation_requested = True
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    new_done, not_done = wait(not_done, timeout=min(remaining, 0.05))
                    done.update(new_done)
                if not_done:
                    for future in not_done:
                        future.cancel()
                        model_id = futures[future]
                        results[model_id] = (
                            self._cancelled(model_id, observed_at)
                            if cancellation_requested
                            else self._record_failure(
                                model_id, observed_at, now_mono, "timeout", "probe timed out"
                            )
                        )
                    for remaining_model in remaining_models:
                        results[remaining_model] = self._record_failure(
                            remaining_model, observed_at, now_mono, "timeout", "probe timed out"
                        )
                    remaining_models = []
                if cancellation_requested:
                    for future in done:
                        future.cancel()
                        results[futures[future]] = self._cancelled(futures[future], observed_at)
                else:
                    for future in done:
                        model_id = futures[future]
                        try:
                            response, latency_ms = future.result()
                            observation = self._record_response(
                                model_id, observed_at, now_mono, response, latency_ms
                            )
                            response_bytes += observation.response_bytes or 0
                            observed_tokens += _probe_token_count(observation)
                            if response_bytes > selected_budget.max_response_bytes:
                                observation = self._budget_limited(model_id, observed_at)
                            results[model_id] = observation
                        except Exception as exc:  # transport failures are data, not runner failures
                            http_status = (
                                exc.code if isinstance(exc, urllib.error.HTTPError) else None
                            )
                            error_class = _error_class(exc)
                            results[model_id] = self._record_failure(
                                model_id,
                                observed_at,
                                now_mono,
                                error_class,
                                _safe_error(exc),
                                http_status=http_status,
                            )
                            if error_class == "oversized_response":
                                response_bytes = selected_budget.max_response_bytes
                            if _error_class(exc) == "oversized_response":
                                response_bytes = selected_budget.max_response_bytes
                if cancellation_requested:
                    for remaining_model in remaining_models:
                        results[remaining_model] = self._cancelled(remaining_model, observed_at)
                    remaining_models = []
                elif (
                    response_bytes >= selected_budget.max_response_bytes
                    or observed_tokens >= selected_budget.max_tokens
                ):
                    for remaining_model in remaining_models:
                        results[remaining_model] = self._budget_limited(
                            remaining_model, observed_at
                        )
                    remaining_models = []
                elif not_done:
                    # A timeout ends the bounded run; no later request may be scheduled.
                    for remaining_model in remaining_models:
                        results[remaining_model] = self._record_failure(
                            remaining_model, observed_at, now_mono, "timeout", "probe timed out"
                        )
                    remaining_models = []
            executor.shutdown(wait=False, cancel_futures=True)

        observations = tuple(results[model_id] for model_id in bounded)
        finished_at = datetime.now(timezone.utc)
        statuses: dict[str, int] = {}
        for observation in observations:
            statuses[observation.status] = statuses.get(observation.status, 0) + 1
        diagnostics = ProbeRunDiagnostics(
            provider=provider,
            live=live,
            consented=consented,
            max_requests=selected_budget.max_requests,
            max_tokens=selected_budget.max_tokens,
            max_response_bytes=selected_budget.max_response_bytes,
            max_duration_seconds=min(
                self.policy.timeout_seconds,
                self.policy.max_duration_seconds,
                selected_budget.max_duration_seconds,
            ),
            requested_models=requested_count,
            unique_models=len(unique),
            bounded_models=len(bounded),
            scheduled_models=scheduled_models,
            transport_calls=transport_calls,
            skipped_models=sum(observation.status == "skipped" for observation in observations),
            cancelled_models=sum(observation.status == "cancelled" for observation in observations),
            budget_limited_models=sum(
                observation.error == "budget_exhausted" for observation in observations
            ),
            observed_tokens=observed_tokens,
            response_bytes=response_bytes,
            statuses=statuses,
            started_at=started_at,
            finished_at=finished_at,
            cancellation_requested=cancellation_requested,
        )
        return ProbeRun(observations=observations, diagnostics=diagnostics)

    def _invoke(self, transport: ProbeTransport, model_id: str) -> tuple[Any, float]:
        started = time.monotonic()
        response = transport(model_id, self.policy.payload(model_id), self.policy.timeout_seconds)
        return response, (time.monotonic() - started) * 1000

    def _cancelled(self, model_id: str, observed_at: datetime) -> ProbeObservation:
        return ProbeObservation(
            model_id=model_id,
            availability_state="degraded",
            status="cancelled",
            observed_at=observed_at,
            error="cancelled",
        )

    def _budget_limited(self, model_id: str, observed_at: datetime) -> ProbeObservation:
        return ProbeObservation(
            model_id=model_id,
            availability_state="degraded",
            status="skipped",
            observed_at=observed_at,
            error="budget_exhausted",
        )

    def _skipped(
        self, model_id: str, observed_at: datetime, now_mono: float, state: _ProbeState, reason: str
    ) -> ProbeObservation:
        quarantined = reason == "quarantined"
        until = state.quarantine_until if quarantined else state.next_probe_at
        return ProbeObservation(
            model_id=model_id,
            availability_state="denied" if quarantined else "degraded",
            status="skipped",
            observed_at=observed_at,
            error=reason,
            consecutive_failures=state.consecutive_failures,
            next_probe_at=_datetime_from_mono(until, observed_at, now_mono),
            quarantine_until=(
                _datetime_from_mono(state.quarantine_until, observed_at, now_mono)
                if quarantined
                else None
            ),
        )

    def _record_response(
        self,
        model_id: str,
        observed_at: datetime,
        now_mono: float,
        response: Any,
        latency_ms: float,
    ) -> ProbeObservation:
        status_code, body, response_bytes = _response_parts(
            response, max_response_bytes=self.policy.max_response_bytes
        )
        usage = body.get("usage") if isinstance(body, Mapping) else None
        usage_available = isinstance(usage, Mapping) and any(
            type(value) is int and value > 0
            for key in ("total_tokens", "completion_tokens", "prompt_tokens")
            for value in (usage.get(key),)
        )
        completion_available = _has_assistant_output(body)
        state = self.registry.state(model_id)
        if status_code is None:
            return self._record_failure(
                model_id,
                observed_at,
                now_mono,
                "malformed_response",
                "upstream response missing integer HTTP status",
                latency_ms=latency_ms,
                response_bytes=response_bytes,
            )
        if status_code is not None and not 200 <= status_code < 300:
            return self._record_failure(
                model_id,
                observed_at,
                now_mono,
                _status_error_class(status_code),
                f"upstream returned HTTP {status_code}",
                http_status=status_code,
                latency_ms=latency_ms,
                response_bytes=response_bytes,
            )
        if not usage_available:
            state.last_state = "degraded"
            state.next_probe_at = now_mono + self.policy.cooldown_seconds
            return ProbeObservation(
                model_id=model_id,
                availability_state="degraded",
                status="usage_unavailable",
                observed_at=observed_at,
                latency_ms=round(latency_ms, 3),
                http_status=status_code,
                error="usage unavailable",
                consecutive_failures=state.consecutive_failures,
                next_probe_at=_datetime_from_mono(state.next_probe_at, observed_at, now_mono),
                response_bytes=response_bytes,
            )
        if not isinstance(usage, Mapping):
            return self._record_failure(
                model_id,
                observed_at,
                now_mono,
                "malformed_response",
                "upstream response usage is malformed",
                http_status=status_code,
                latency_ms=latency_ms,
                response_bytes=response_bytes,
            )
        if not completion_available:
            state.last_state = "degraded"
            state.next_probe_at = now_mono + self.policy.cooldown_seconds
            return ProbeObservation(
                model_id=model_id,
                availability_state="degraded",
                status="completion_unavailable",
                observed_at=observed_at,
                latency_ms=round(latency_ms, 3),
                usage_available=True,
                prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
                completion_tokens=_int_or_none(usage.get("completion_tokens")),
                total_tokens=_int_or_none(usage.get("total_tokens")),
                http_status=status_code,
                error="assistant completion unavailable",
                consecutive_failures=state.consecutive_failures,
                next_probe_at=_datetime_from_mono(state.next_probe_at, observed_at, now_mono),
                response_bytes=response_bytes,
            )
        state.consecutive_failures = 0
        state.last_state = "ready"
        state.next_probe_at = now_mono + self.policy.cooldown_seconds
        return ProbeObservation(
            model_id=model_id,
            availability_state="ready",
            status="ready",
            observed_at=observed_at,
            latency_ms=round(latency_ms, 3),
            usage_available=True,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            total_tokens=_int_or_none(usage.get("total_tokens")),
            http_status=status_code,
            consecutive_failures=0,
            next_probe_at=_datetime_from_mono(state.next_probe_at, observed_at, now_mono),
            response_bytes=response_bytes,
        )

    def _record_failure(
        self,
        model_id: str,
        observed_at: datetime,
        now_mono: float,
        error_class: str,
        error: str,
        *,
        http_status: int | None = None,
        latency_ms: float | None = None,
        response_bytes: int | None = None,
    ) -> ProbeObservation:
        state = self.registry.state(model_id)
        state.consecutive_failures += 1
        delay = min(
            self.policy.max_cooldown_seconds,
            self.policy.cooldown_seconds * (2 ** max(0, state.consecutive_failures - 1)),
        )
        state.next_probe_at = now_mono + delay
        quarantined = state.consecutive_failures >= self.policy.failure_threshold
        if quarantined:
            state.quarantine_until = now_mono + self.policy.quarantine_seconds
            state.last_state = "denied"
            availability_state = "denied"
        else:
            state.last_state = "degraded"
            availability_state = "degraded"
        return ProbeObservation(
            model_id=model_id,
            availability_state=availability_state,
            status=(
                "timeout"
                if error_class == "timeout"
                else "oversized_response"
                if error_class == "oversized_response"
                else "failed"
            ),
            observed_at=observed_at,
            latency_ms=round(latency_ms, 3) if latency_ms is not None else None,
            http_status=http_status,
            error_class=error_class,
            error=error,
            consecutive_failures=state.consecutive_failures,
            next_probe_at=_datetime_from_mono(state.next_probe_at, observed_at, now_mono),
            quarantine_until=_datetime_from_mono(state.quarantine_until, observed_at, now_mono)
            if quarantined
            else None,
            response_bytes=response_bytes,
        )


def _response_parts(
    response: Any, *, max_response_bytes: int
) -> tuple[int | None, Mapping[str, Any], int]:
    if isinstance(response, Mapping):
        status = response.get("status_code", 200)
        body = response.get("body", response)
        if isinstance(body, (bytes, bytearray)):
            response_bytes = len(body)
            if response_bytes > max_response_bytes:
                raise ProbeResponseTooLargeError("response exceeds the configured byte limit")
            try:
                body = json.loads(body)
            except (TypeError, ValueError, json.JSONDecodeError):
                body = {}
        else:
            try:
                response_bytes = len(
                    json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
            except (TypeError, ValueError):
                response_bytes = 0
            if response_bytes > max_response_bytes:
                raise ProbeResponseTooLargeError("response exceeds the configured byte limit")
        return (
            int(status) if isinstance(status, int) else None,
            body if isinstance(body, Mapping) else {},
            response_bytes,
        )
    status = getattr(response, "status_code", 200)
    response_bytes = 0
    try:
        body = response.json()
    except Exception:
        body = {}
    try:
        response_bytes = len(
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except (TypeError, ValueError):
        response_bytes = 0
    if response_bytes > max_response_bytes:
        raise ProbeResponseTooLargeError("response exceeds the configured byte limit")
    return (
        int(status) if isinstance(status, int) else None,
        body if isinstance(body, Mapping) else {},
        response_bytes,
    )


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _probe_token_count(observation: ProbeObservation) -> int:
    """Return conservative output-token accounting for one fixed probe."""

    if observation.usage_available:
        if observation.completion_tokens is not None and observation.completion_tokens > 0:
            return observation.completion_tokens
        if observation.total_tokens is not None and observation.total_tokens > 0:
            return observation.total_tokens
    return 1


def _has_assistant_output(body: Mapping[str, Any]) -> bool:
    """Return whether a non-streaming chat response contains assistant output."""
    choices = body.get("choices")
    if not isinstance(choices, Sequence) or isinstance(choices, (str, bytes, bytearray)):
        return False
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if (
            isinstance(message, Mapping)
            and message.get("role") == "assistant"
            and _has_content(message.get("content"))
        ):
            return True
    return False


def _has_content(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, Sequence) or isinstance(content, (bytes, bytearray)):
        return False
    for part in content:
        if isinstance(part, str) and part.strip():
            return True
        if not isinstance(part, Mapping):
            continue
        for key in ("text", "content"):
            value = part.get(key)
            if isinstance(value, str) and value.strip():
                return True
    return False


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, urllib.error.HTTPError):
        return _status_error_class(exc.code)
    if isinstance(exc, ProbeResponseTooLargeError):
        return "oversized_response"
    return type(exc).__name__.lower()


def _status_error_class(status: int) -> str:
    if status in {401, 403}:
        return "unauthorized"
    if status == 402:
        return "quota_exhausted"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "upstream_error"
    return "http_error"


_SECRET_RE = re.compile(r"(?i)(bearer\s+|(?:sk|key|token|secret)[-_:=\s]+)([^\s,;)}\]]+)")
_URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s?]+\?[^\s]+)")


def _safe_error(exc: BaseException) -> str:
    return _redact(str(exc))[:300] or type(exc).__name__.lower()


def _redact(value: str) -> str:
    value = _SECRET_RE.sub(r"\1[REDACTED]", value)
    return _URL_QUERY_RE.sub("[REDACTED_URL]", value)


def _datetime_from_mono(target: float, reference: datetime, current_mono: float) -> datetime | None:
    if target <= 0:
        return None
    # Anchor relative monotonic deadlines to the caller's observation timestamp.
    return reference + timedelta(seconds=max(0.0, target - current_mono))


def _format_datetime(value: datetime) -> str:
    """Serialize an aware timestamp in stable UTC form."""

    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def openai_probe_transport(
    base_url: str,
    *,
    api_key: str | None = None,
    opener: Any = urllib.request.urlopen,
    max_response_bytes: int = 1_048_576,
    session_id: str | None = None,
) -> ProbeTransport:
    """Create a small stdlib OpenAI-compatible transport.

    ``api_key`` is supplied explicitly and never logged.  This helper does not
    read environment variables, making credential flow auditable at the caller.
    ``session_id`` is sent as ``X-Session-Id`` when provided.
    """
    if max_response_bytes < 1:
        raise ValueError("max_response_bytes must be positive")
    endpoint = base_url.rstrip("/") + "/chat/completions"

    def transport(
        model_id: str, payload: Mapping[str, Any], timeout_seconds: float
    ) -> Mapping[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if session_id:
            headers["X-Session-Id"] = session_id
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with opener(request, timeout=timeout_seconds) as response:
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise ProbeResponseTooLargeError("response exceeds the configured byte limit")
            body = json.loads(raw) if raw else {}
            return {"status_code": response.status, "body": body}

    return transport
