"""Owned worker attempts: admission -> terminal validation -> replacement.

The CLI runs in Verdict's environment. Prime's small kernel bridge supplies only
native spawn/collect/delete RPCs; provider errors never unwind the controller.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import stat
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from verdict.admission import AdmittedSet, active_controller_route, load_live_admission
from verdict.availability import (
    AvailabilityCandidate,
    AvailabilityReport,
    AvailabilityState,
    CandidateRequirements,
    OmniRouteAvailabilityAdapter,
    StaticOmniRouteTransport,
)
from verdict.omniroute import OmniRouteHTTPTransport
from verdict.orchestration.effort import PRIME_THINKING_LEVELS, choose_effort, effort_reason
from verdict.repository_files import hold_repository_dirs
from verdict.subagent_selection import (
    CONTEXT_LENGTH_CATEGORY,
    DEFAULT_OMNIROUTE_URL,
    PROVIDER_SCOPE_FAILURE_CATEGORIES,
    HealthCache,
    HealthResult,
    LaunchCandidate,
    WorkerTask,
    WorkerTerminal,
    _failure_cooldown,
    classify_worker_failure,
    eligible_worker_candidates,
    openai_health_probe,
)

# Source label for the bounded live confirmation the worker path runs before launch.
CONFIRMATION_SOURCE = "worker_probe:openai_health_probe"


@dataclass(frozen=True)
class RuntimeBudget:
    # Unique candidates are the default attempt bound; wall time includes probes.
    total_seconds: float = 900
    attempt_seconds: float = 180
    probe_seconds: float = 15
    cleanup_seconds: float = 30
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if (
            min(self.total_seconds, self.attempt_seconds, self.probe_seconds, self.cleanup_seconds)
            <= 0
        ):
            raise ValueError("runtime deadlines must be positive")
        if self.max_attempts is not None and self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


@dataclass(frozen=True)
class WorkerOutcome:
    state: str
    output: str
    diagnostic: str
    candidate: LaunchCandidate | None
    spawn_id: str | None
    attempts: tuple[tuple[str, str], ...]

    def render(self) -> str:
        if self.state == "SUCCESS" and self.candidate:
            return (
                f"SUCCESS model={self.candidate.selector} spawn_id={self.spawn_id}\n{self.output}"
            )
        return f"FAIL_CLOSED {self.diagnostic}"


class WorkerAdapter(Protocol):
    async def spawn(
        self, prompt: str, *, name: str, model: str, thinking: str | None = None
    ) -> Mapping[str, Any]: ...
    async def collect(self, handle: Mapping[str, Any]) -> WorkerTerminal | None: ...
    async def delete(self, handle: Mapping[str, Any]) -> None: ...


class ReceiptPersistenceError(RuntimeError):
    """Local durable receipt failure, never evidence of route/provider ill-health."""


class AttemptFailureError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def configured_runtime_sources(
    *, management_token: str | None, usage_api_key_id: str | None
) -> frozenset[str]:
    """Use every documented availability source that current credentials permit."""
    sources = {"health"}
    if management_token and management_token.strip():
        sources.update({"rate_limits", "model_cooldowns"})
        if usage_api_key_id and usage_api_key_id.strip():
            sources.update({"budget", "token_limits"})
    return frozenset(sources)


def _csv_frozenset(value: str | None) -> frozenset[str]:
    return frozenset(item.strip() for item in (value or "").split(",") if item.strip())


def _route_id(value: str) -> str:
    return value.strip().removeprefix("omniroute/")


def worker_task_from_config(raw: Mapping[str, Any] | None) -> WorkerTask:
    """Normalize durable task config and fence the active controller identity."""
    config = dict(raw or {})
    for key in ("required_capabilities", "allowed_route_prefixes", "excluded_route_ids"):
        config[key] = frozenset(
            str(item).strip() for item in config.get(key, ()) if str(item).strip()
        )

    if not config["allowed_route_prefixes"]:
        config["allowed_route_prefixes"] = _csv_frozenset(
            os.environ.get("VERDICT_WORKER_ROUTE_PREFIXES")
        )

    if "max_capability_tier" in config:
        tier = config["max_capability_tier"]
        if type(tier) is not int or not 0 <= tier <= 3:
            raise ValueError("max_capability_tier must be an integer 0..3")

    excluded = set(config["excluded_route_ids"])
    controller = os.environ.get("VERDICT_CONTROLLER_MODEL", "").strip()
    if controller:
        excluded.add(_route_id(controller))
    config["excluded_route_ids"] = frozenset(excluded)
    return WorkerTask(**config)


def _has_measured_usage(candidate: AvailabilityCandidate) -> bool:
    """True only when runtime/probe evidence measured usable capacity."""
    if candidate.headroom_pct is not None:
        return True
    token_headroom = candidate.normalized.get("token_headroom")
    if type(token_headroom) is int:
        return True
    # A READY probe is only possible when probe metadata explicitly says
    # usage_available=true; _probe_state validates that contract before READY.
    return candidate.source == "verdict:probe" and candidate.state.value == "eligible"


def admitted_worker_candidates(
    task: WorkerTask,
    inventory_rows: Iterable[Mapping[str, Any]],
    prime_selectors: Iterable[str],
    availability: AvailabilityReport,
    *,
    require_usage_evidence: bool = True,
) -> tuple[LaunchCandidate, ...]:
    """Narrow the canonical availability verdict; never locally re-admit a route."""
    admitted_ids = {
        candidate.model.id
        for candidate in availability.eligible
        if not require_usage_evidence or _has_measured_usage(candidate)
    }
    if not require_usage_evidence:
        # A route whose ONLY shortfall is that the gateway reported no health
        # for it may enter the PRE-PROBE pool (documented intent below and in
        # BOD-262): it is not launchable yet. Canonical admission marks it
        # unconfirmed and WorkerController runs the bounded live confirmation
        # probe for that exact route before any spawn. Any measured problem
        # (quota, rate limit, auth, lockout, circuit, cooldown) stays excluded.
        admitted_ids |= {
            candidate.model.id
            for candidate in availability.candidates
            if candidate.state is AvailabilityState.UNKNOWN
            and candidate.reasons == ("health unknown",)
        }
    narrowed = [
        row
        for row in inventory_rows
        if isinstance(row.get("id"), str) and str(row["id"]) in admitted_ids
    ]
    return eligible_worker_candidates(task, narrowed, prime_selectors)


def _worker_requirements(task: WorkerTask) -> CandidateRequirements:
    required = set(task.required_capabilities)
    if task.reasoning:
        required.add("reasoning")
    # Worker execution changes code/state. Unknown/degraded runtime truth is not
    # sufficient final admission evidence. Unknown usage may enter the pre-probe
    # pool, but WorkerController requires a fresh/cache-valid inference probe before spawn.
    return CandidateRequirements(required=frozenset(required), protected=True)


def _catalog_rows(payload: Any) -> tuple[Mapping[str, Any], ...]:
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Sequence) or isinstance(data, (str, bytes, bytearray)):
        raise ValueError("OmniRoute /v1/models response has no data array")
    return tuple(row for row in data if isinstance(row, Mapping))


def _availability_evidence(report: AvailabilityReport) -> list[dict[str, Any]]:
    eligible = {item.model.id for item in report.eligible}
    return [
        {
            "model": item.model.id,
            "state": item.state.value,
            "reasons": list(item.reasons),
            "headroom_pct": item.headroom_pct,
            "usage_measured": _has_measured_usage(item),
            "admitted_by_availability": item.model.id in eligible,
        }
        for item in report.candidates
    ]


def validate_terminal(value: object) -> str:
    if not isinstance(value, WorkerTerminal):
        raise AttemptFailureError("malformed_result")
    if value.error:
        raise RuntimeError(value.error)
    if value.state != "done":
        raise AttemptFailureError("child_" + value.state)
    if not value.replied:
        raise AttemptFailureError("child_without_reply")
    if value.stop_reason != "stop":
        raise AttemptFailureError("malformed_result")
    if not isinstance(value.output, str) or not value.output.strip():
        raise AttemptFailureError("empty_output")
    return value.output.strip()


def _append_spawn_receipt(directory: Path, row: Mapping[str, Any]) -> None:
    """Append to an owner-controlled run directory without following any links.

    Held directory descriptors reuse the repository path walk. The run directory
    follows Prime's private-parent policy; shared ancestors such as /tmp are OK.
    Advisory locks serialize cooperating writers across the complete durable line.
    """
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("unsafe receipt directory")
    relative = str(directory / "spawn-receipts.jsonl").removeprefix("/")
    with hold_repository_dirs("/", relative) as (parent_fd, leaf, _):
        info = os.fstat(parent_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("unsafe receipt directory")
        # The held O_PATH directory cannot fsync; open that exact directory inode.
        sync_fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
            created = False
            try:
                fd = os.open(leaf, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                fd = os.open(leaf, flags, dir_fd=parent_fd)
            try:

                def validate() -> None:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_nlink != 1
                    ):
                        raise ValueError("unsafe receipt file")

                validate()  # Refuse special files before attempting any lock.
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    validate()
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "a", encoding="utf-8", closefd=False) as stream:
                        stream.write(json.dumps(row) + "\n")
                        stream.flush()
                        os.fsync(fd)
                    if created:
                        os.fsync(sync_fd)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        finally:
            os.close(sync_fd)


class WorkerController:
    """One immutable task, one owned child at a time, exactly one final outcome."""

    def __init__(
        self,
        task: WorkerTask,
        *,
        inventory_rows: Iterable[Mapping[str, Any]],
        prime_selectors: Iterable[str],
        probe: Callable[[LaunchCandidate], HealthResult],
        adapter: WorkerAdapter,
        cache: HealthCache,
        budget: RuntimeBudget = RuntimeBudget(),
        now: Callable[[], datetime] | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
        validator: Callable[[str], bool] | None = None,
        admitted: AdmittedSet | None = None,
        require_admission: bool = False,
        run_dir: Path | None = None,
    ) -> None:
        # The canonical admitted set is applied before Prime visibility, ranking
        # and probing; replacements iterate this same narrowed list only.
        self.admitted = admitted
        self.task_kind = task.task_kind
        self.run_dir = run_dir
        inventory_rows = tuple(inventory_rows)
        self.model_rows = {str(row.get("id", "")): row for row in inventory_rows}
        self.candidates = eligible_worker_candidates(
            task,
            inventory_rows,
            prime_selectors,
            admitted=admitted,
            require_admission=require_admission,
        )
        self.probe, self.adapter, self.cache, self.budget = probe, adapter, cache, budget
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.emit = emit or (lambda event: None)
        self.validator = validator or (lambda output: bool(output.strip()))
        self.attempts: list[tuple[str, str]] = []
        self.events: list[dict[str, Any]] = []
        self.spawn_receipts: list[dict[str, Any]] = []
        self.outcome: WorkerOutcome | None = None
        self.operation_id = uuid.uuid4().hex

    def event(self, event: str, **fields: Any) -> None:
        row = {"event": event, "operation_id": self.operation_id, **fields}
        self.events.append(row)
        self.emit(row)

    def spawn_receipt(
        self,
        attempt: int,
        candidate: LaunchCandidate,
        thinking: str | None,
        reason: Mapping[str, Any],
        handle: Mapping[str, Any] | None,
    ) -> None:
        """One admission-time record per spawn attempt, including failed admissions."""
        executed_effort = handle.get("thinking") if handle is not None else None
        row = {
            "schema": "verdict.spawn-receipt/v1",
            "operation_id": self.operation_id,
            "attempt": attempt,
            "at": self.now().isoformat(),
            "chosen_model": candidate.selector,
            "chosen_effort": thinking,
            "reason": dict(reason),
            "executed_model": handle.get("model") if handle is not None else None,
            "executed_effort": executed_effort
            if executed_effort in PRIME_THINKING_LEVELS
            else "unverified",
            "spawn_id": handle.get("rlm_child_id") if handle is not None else None,
        }
        self.spawn_receipts.append(row)
        if self.run_dir is not None:
            _append_spawn_receipt(self.run_dir, row)

    def finish(
        self,
        state: str,
        diagnostic: str = "",
        *,
        output: str = "",
        candidate: LaunchCandidate | None = None,
        spawn_id: str | None = None,
    ) -> WorkerOutcome:
        if self.outcome is None:
            self.outcome = WorkerOutcome(
                state, output, diagnostic, candidate, spawn_id, tuple(self.attempts)
            )
            self.event(
                "final",
                state=state,
                diagnostic=diagnostic,
                spawn_id=spawn_id,
                final_successful_model=candidate.selector if candidate else None,
            )
        return self.outcome

    async def run(self, prompt: str) -> WorkerOutcome:
        if self.outcome is not None:
            return self.outcome
        try:
            return await self._run(prompt)
        except Exception as exc:
            return self.finish(
                "FAIL_CLOSED",
                f"controller infrastructure: {type(exc).__name__}: {exc}; "
                "inspect events and restore registry/cache/bridge access",
            )

    async def _run(self, prompt: str) -> WorkerOutcome:
        deadline = time.monotonic() + self.budget.total_seconds
        previous: str | None = None
        blocked_providers: dict[str, str] = {}
        # BOD-272: after a context-length overflow the SAME prompt cannot fit a
        # route with a strictly smaller advertised window. Skip those instead
        # of blindly replaying the oversized request. Equal windows stay
        # eligible: advertised windows are not reliable limits (a 1M-advertised
        # route was observed failing near 200k), so a same-size route on
        # another backend may still fit.
        overflowed_at: int | None = None
        for candidate in self.candidates:
            provider = candidate.route_id.split("/", 1)[0].strip().lower()
            if overflowed_at is not None and candidate.context_tokens < overflowed_at:
                self.event(
                    "exclusion",
                    model=candidate.selector,
                    provider=provider,
                    classification="context_too_small",
                    context_tokens=candidate.context_tokens,
                    overflowed_at=overflowed_at,
                    provider_wide=False,
                    replacement=True,
                )
                previous = candidate.selector
                continue
            blocked_reason = blocked_providers.get(provider)
            if blocked_reason is not None:
                self.event(
                    "exclusion",
                    model=candidate.selector,
                    provider=provider,
                    classification=blocked_reason,
                    provider_wide=True,
                    replacement=True,
                )
                previous = candidate.selector
                continue
            if time.monotonic() >= deadline:
                return self.finish(
                    "FAIL_CLOSED",
                    "total time budget reached; inspect attempt events; "
                    "increase total_seconds or restore provider capacity",
                )
            if (
                self.budget.max_attempts is not None
                and len(self.attempts) >= self.budget.max_attempts
            ):
                return self.finish(
                    "FAIL_CLOSED",
                    "replacement budget exhausted; increase max_attempts "
                    "or restore provider capacity",
                )
            health = self.cache.usable(candidate.selector, now=self.now())
            # Launch gate: an admitted route that is not proven healthy needs a
            # bounded live confirmation (this probe) for this exact route first.
            # A cached healthy hit is not proof for an unverified route.
            confirming = (
                self.admitted is not None
                and not self.admitted.launchable(candidate.route_id)
                and (health is None or health.healthy)
            )
            if confirming:
                health = None
            if health is None:
                try:
                    health = await asyncio.wait_for(
                        asyncio.to_thread(self.probe, candidate),
                        min(self.budget.probe_seconds, deadline - time.monotonic()),
                    )
                    if not isinstance(health, HealthResult):
                        raise AttemptFailureError("malformed_probe")
                except Exception as exc:
                    health = self.failure(exc)
                if health.healthy:
                    self.cache.record(candidate.selector, health, now=self.now())
                else:
                    self.cache.record_failure(candidate, health, now=self.now())
                if confirming and self.admitted is not None:
                    observed = self.now().isoformat()
                    self.admitted = self.admitted.record_confirmation(
                        candidate.route_id,
                        healthy=health.healthy,
                        source=CONFIRMATION_SOURCE,
                        observed_at=observed,
                        category=health.category,
                    )
                    self.event(
                        "confirmation",
                        model=candidate.selector,
                        source=CONFIRMATION_SOURCE,
                        observed_at=observed,
                        confirmed=health.healthy,
                        classification=health.category,
                    )
            self.event(
                "health",
                model=candidate.selector,
                provider=provider,
                classification=health.category,
                eligible=health.healthy,
            )
            if not health.healthy:
                provider_wide = health.category in PROVIDER_SCOPE_FAILURE_CATEGORIES
                if provider_wide:
                    blocked_providers[provider] = health.category
                self.event(
                    "exclusion",
                    model=candidate.selector,
                    provider=provider,
                    classification=health.category,
                    cooldown_seconds=_failure_cooldown(health),
                    provider_wide=provider_wide,
                    replacement=True,
                )
                previous = candidate.selector
                continue
            if self.admitted is not None:
                # Asserted precondition: admitted AND (proven healthy OR confirmed).
                self.admitted.require_launchable(candidate.route_id, surface="worker_runtime.spawn")
            number = len(self.attempts) + 1
            model_row = self.model_rows.get(candidate.route_id, {})
            thinking = choose_effort(self.task_kind, model_row)
            reason = effort_reason(self.task_kind, thinking, model_row)
            self.event(
                "selection",
                attempt=number,
                model=candidate.selector,
                provider=provider,
                previous_model=previous,
                replacement_model=candidate.selector if previous else None,
                chosen_model=candidate.selector,
                chosen_effort=thinking,
                reason=reason,
            )
            handle: Mapping[str, Any] | None = None
            spawn_id: str | None = None
            name = f"verdict-{self.operation_id[:10]}-{number}"
            admission_pending = True
            spawn_timed_out = False
            try:
                attempt_deadline = min(deadline, time.monotonic() + self.budget.attempt_seconds)
                try:
                    handle = await asyncio.wait_for(
                        self.adapter.spawn(
                            prompt, name=name, model=candidate.selector, thinking=thinking
                        ),
                        max(0.001, attempt_deadline - time.monotonic()),
                    )
                    admission_pending = False
                except TimeoutError:
                    spawn_timed_out = True
                    raise
                finally:
                    try:
                        self.spawn_receipt(number, candidate, thinking, reason, handle)
                    except Exception as receipt_error:
                        raise ReceiptPersistenceError(
                            "local_persistence_failure"
                        ) from receipt_error
                spawn_id = handle.get("rlm_child_id")
                if (
                    not isinstance(spawn_id, str)
                    or not spawn_id
                    or handle.get("model") != candidate.selector
                ):
                    raise AttemptFailureError("malformed_admission")
                self.event(
                    "admission",
                    attempt=number,
                    model=candidate.selector,
                    spawn_id=spawn_id,
                    admitted=True,
                    executed_model=handle.get("model"),
                    executed_effort=self.spawn_receipts[-1]["executed_effort"],
                )
                while True:
                    remaining = attempt_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("child execution deadline")
                    terminal = await asyncio.wait_for(self.adapter.collect(handle), remaining)
                    if terminal is not None:
                        self.event(
                            "terminal",
                            attempt=number,
                            model=candidate.selector,
                            spawn_id=spawn_id,
                            child_state=getattr(terminal, "state", "malformed"),
                            replied=getattr(terminal, "replied", False),
                        )
                        output = validate_terminal(terminal)
                        if not self.validator(output):
                            raise AttemptFailureError("invalid_output")
                        break
                    await asyncio.sleep(min(0.1, remaining))
            except Exception as exc:
                if admission_pending and spawn_timed_out:
                    # Spawn may have been admitted after the RPC deadline. Reap by
                    # our unique name before permitting another writer.
                    handle = {"rlm_child_id": name, "model": candidate.selector}
                local_persistence = isinstance(exc, ReceiptPersistenceError)
                health = (
                    HealthResult(False, "local_persistence_failure")
                    if local_persistence
                    else self.failure(exc)
                )
                if handle is not None:
                    reported_id = handle.get("rlm_child_id")
                    spawn_id = reported_id if isinstance(reported_id, str) else None
                provider_wide = health.category in PROVIDER_SCOPE_FAILURE_CATEGORIES
                if provider_wide:
                    blocked_providers[provider] = health.category
                if health.category == CONTEXT_LENGTH_CATEGORY:
                    window = candidate.context_tokens
                    overflowed_at = window if overflowed_at is None else max(overflowed_at, window)
                self.attempts.append((candidate.selector, health.category))
                if not local_persistence:
                    self.cache.record_failure(candidate, health, now=self.now())
                self.event(
                    "failure",
                    attempt=number,
                    model=candidate.selector,
                    provider=provider,
                    spawn_id=spawn_id,
                    classification=health.category,
                    cooldown_seconds=0 if local_persistence else _failure_cooldown(health),
                    provider_wide=provider_wide,
                    excluded=not local_persistence,
                    replacement=not local_persistence,
                )
                if handle is not None:
                    # A timed-out writer must be reaped before a replacement can write.
                    try:
                        await asyncio.wait_for(
                            self.adapter.delete(handle), self.budget.cleanup_seconds
                        )
                    except Exception as cleanup:
                        return self.finish(
                            "FAIL_CLOSED",
                            f"cleanup_unconfirmed spawn={spawn_id}: "
                            f"{cleanup}; stop this owned child before retrying",
                        )
                if local_persistence:
                    return self.finish(
                        "FAIL_CLOSED",
                        "local_persistence_failure: owned child reaped; "
                        "restore safe receipt storage before retrying (no route cooldown)",
                    )
                previous = candidate.selector
                continue
            self.attempts.append((candidate.selector, "completed"))
            self.cache.record(candidate.selector, HealthResult(True, "healthy"), now=self.now())
            return self.finish("SUCCESS", output=output, candidate=candidate, spawn_id=spawn_id)
        detail = ", ".join(f"{model}:{category}" for model, category in self.attempts)
        return self.finish(
            "FAIL_CLOSED",
            "eligible candidates exhausted (including active cooldowns); "
            f"restore provider health or refresh discovery; attempts={detail}",
        )

    @staticmethod
    def failure(exc: Exception) -> HealthResult:
        if isinstance(exc, AttemptFailureError):
            return HealthResult(False, exc.category)
        return classify_worker_failure(exc)


class CallbackAdapter:
    """Compatibility adapter for terminal-producing callbacks, never spawn callbacks."""

    def __init__(self, execute: Callable[[str], Awaitable[WorkerTerminal]]) -> None:
        self.execute = execute

    async def spawn(
        self, prompt: str, *, name: str, model: str, thinking: str | None = None
    ) -> Mapping[str, Any]:
        return {"rlm_child_id": name, "model": model}

    async def collect(self, handle: Mapping[str, Any]) -> WorkerTerminal:
        return await self.execute(str(handle["model"]))

    async def delete(self, handle: Mapping[str, Any]) -> None:
        return None


def atomic_json(path: Path, value: object) -> None:
    temp = path.with_suffix(".tmp")

    def encode(item: Any) -> Any:
        if isinstance(item, (set, frozenset)):
            return sorted(item)
        if isinstance(item, Path):
            return str(item)
        raise TypeError(f"unsupported result field {type(item).__name__}")

    temp.write_text(json.dumps(value, default=encode), encoding="utf-8")
    temp.replace(path)


class PrimeFileAdapter:
    """RPC transport to the parent kernel; no provider inference in the parent turn."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    async def rpc(self, method: str, **params: Any) -> Any:
        request_id = uuid.uuid4().hex
        atomic_json(self.directory / "request.json", {"id": request_id, "method": method, **params})
        while True:
            response_path = self.directory / "response.json"
            if response_path.exists():
                response = json.loads(response_path.read_text())
                if response.get("id") == request_id:
                    if response.get("error"):
                        raise RuntimeError(response["error"])
                    return response.get("value")
            await asyncio.sleep(0.05)

    async def spawn(
        self, prompt: str, *, name: str, model: str, thinking: str | None = None
    ) -> Mapping[str, Any]:
        value = await self.rpc("spawn", prompt=prompt, name=name, model=model, thinking=thinking)
        if not isinstance(value, dict):
            raise AttemptFailureError("malformed_admission")
        return value

    async def collect(self, handle: Mapping[str, Any]) -> WorkerTerminal | None:
        rows = await self.rpc("collect", child_id=handle["rlm_child_id"])
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise AttemptFailureError("malformed_result")
        row = rows[0]
        if row.get("rlm_child_id") != handle["rlm_child_id"]:
            raise AttemptFailureError("malformed_result")
        if row.get("status") in {"running", "queued"} and row.get("settled") is False:
            return None
        if row.get("settled") is not True:
            raise AttemptFailureError("malformed_result")
        if row.get("error"):
            return WorkerTerminal(str(row.get("status")), error=str(row["error"]))
        # collect.answer_preview is truncated and can be from an earlier tool turn.
        # Validate the actual final assistant message in the owned child's journal.
        directory = Path(str(handle["session_dir"]))
        files = []
        for path in directory.glob("*.jsonl"):
            with path.open(encoding="utf-8") as stream:
                first = stream.readline()
            if first and json.loads(first).get("type") == "session":
                files.append(path)
        if len(files) != 1:
            raise AttemptFailureError("missing_terminal_journal")
        messages = [
            json.loads(line).get("message", {}) for line in files[0].read_text().splitlines()
        ]
        assistants = [message for message in messages if message.get("role") == "assistant"]
        if not assistants:
            return WorkerTerminal(
                str(row.get("status")), replied=row.get("replied_since_task") is True
            )
        last = assistants[-1]
        expected = handle.get("model")
        if expected is not None and not last.get("errorMessage"):
            actual = f"{last.get('provider')}/{last.get('model')}"
            if actual != expected:
                raise AttemptFailureError("model_provenance_mismatch")
        output = "\n".join(
            block["text"]
            for block in last.get("content", [])
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
        return WorkerTerminal(
            str(row.get("status")),
            output,
            row.get("replied_since_task") is True,
            last.get("errorMessage"),
            last.get("stopReason"),
        )

    async def delete(self, handle: Mapping[str, Any]) -> None:
        await self.rpc("delete", child_id=handle["rlm_child_id"])


def _bootstrap_gateway_url() -> str | None:
    """Gateway named by the shared bootstrap contract, or ``None`` if it names none.

    Keeps the worker on the same gateway precedence as every other path
    (environment, then config ``gateway_url``, then the gateway provider). A
    configuration fault is not swallowed into a different gateway: it returns
    ``None`` and the explicit runtime config / documented default applies.
    """
    from verdict.provider_bootstrap import (
        BootstrapError,
        load_credential_store_env,
        resolve_provider_bootstrap,
    )

    try:
        bootstrap = resolve_provider_bootstrap(credential_store_env=load_credential_store_env())
    except BootstrapError:
        return None
    return bootstrap.gateway_url


def _worker_admission(config: Mapping[str, Any], rows: Iterable[Mapping[str, Any]]) -> AdmittedSet:
    """Canonical live admission, then worker-only scope and controller exclusion."""
    from verdict.orchestration.run import resolve_api_key

    gateway = str(config.get("gateway") or _bootstrap_gateway_url() or DEFAULT_OMNIROUTE_URL)
    task = config.get("task") or {}
    required = {str(c) for c in task.get("required_capabilities") or ()}
    if task.get("reasoning"):
        required.add("reasoning")
    admitted = load_live_admission(
        gateway,
        now=datetime.now(timezone.utc),
        api_key=resolve_api_key(),
        inventory_rows=list(rows),
        required_capabilities=frozenset(required),
        min_context_tokens=int(task.get("min_context_tokens") or 0),
    )
    prefixes = config.get("route_prefixes") or ()
    if isinstance(prefixes, str):
        prefixes = [p for p in prefixes.split(",") if p.strip()]
    admitted = admitted.restrict_prefixes(list(prefixes))
    return admitted.exclude_controller(
        str(config.get("controller_route") or "") or active_controller_route()
    )


async def cli_run(directory: Path, *, sync_visibility: bool = False) -> int:
    config = json.loads((directory / "config.json").read_text())
    events = directory / "events.jsonl"

    def emit(event: dict[str, Any]) -> None:
        if event["event"] == "final":
            return  # Published exactly once below, after serializing the outcome.
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")

    try:
        task = worker_task_from_config(config.get("task", {}))
        base_url = (
            os.environ.get("OMNIROUTE_BASE_URL")
            or os.environ.get("LLMGATE_UPSTREAM_BASE_URL")
            or "http://127.0.0.1:20128/v1"
        )
        api_key = os.environ.get("OMNIROUTE_API_KEY") or os.environ.get("VERDICT_OMNIROUTE_API_KEY")
        management_token = os.environ.get("OMNIROUTE_MANAGEMENT_TOKEN")
        usage_api_key_id = os.environ.get("OMNIROUTE_USAGE_API_KEY_ID")
        transport = OmniRouteHTTPTransport(
            base_url,
            api_key=api_key,
            management_token=management_token,
            usage_api_key_id=usage_api_key_id,
            runtime_sources=configured_runtime_sources(
                management_token=management_token, usage_api_key_id=usage_api_key_id
            ),
            allow_private_hosts={"127.0.0.1", "::1"},
            max_response_bytes=16_777_216,
        )
        catalog_payload, runtime_payload = await asyncio.gather(
            asyncio.to_thread(transport.catalog), asyncio.to_thread(transport.runtime)
        )
        rows = _catalog_rows(catalog_payload)
        # One catalog GET feeds both the (opt-in) Prime visibility refresh and
        # admission. The snapshot is visibility only; admission and the exact
        # confirmation probe below still decide launch authority.
        #
        # Default launch is read-only: it never writes the operator's Prime
        # registry (openspec bod-293-295 design.md:16,44 -- "Picker must not
        # call automatic visibility refresh or sync implicitly"). Only
        # --sync-visibility opts into the old unconditional write.
        if sync_visibility:
            from verdict.harness_prime import refresh_omniroute_visibility

            visibility = await asyncio.to_thread(
                refresh_omniroute_visibility,
                fetch_rows=lambda: rows,
                source=transport.base_url.rstrip("/") + "/v1/models",
                force=True,
            )
        else:
            from verdict.harness_prime import resolve_paths
            from verdict.prime_inventory import PrimeInventoryStatus, _read, sidecar_path

            registry_paths = resolve_paths()
            snapshot, failure = await asyncio.to_thread(_read, sidecar_path(registry_paths.models))
            visibility = PrimeInventoryStatus(snapshot, failure, refreshed=False, fresh=False)
        # CLI registry listing is complete; find_models has a bounded search limit.
        process = await asyncio.create_subprocess_exec(
            "prime-agent",
            "model",
            "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError("Prime model registry unavailable: " + stderr.decode()[:500])
        selectors = [
            "/".join(line.split()[:2])
            for line in stdout.decode().splitlines()[1:]
            if len(line.split()) >= 2
        ]

        availability = OmniRouteAvailabilityAdapter(
            StaticOmniRouteTransport(catalog_payload, runtime_payload)
        ).evaluate(_worker_requirements(task))
        candidates = admitted_worker_candidates(
            task, rows, selectors, availability, require_usage_evidence=False
        )
        preprobe_ids = {candidate.route_id for candidate in candidates}
        narrowed_rows = [row for row in rows if str(row.get("id", "")) in preprobe_ids]
        atomic_json(
            directory / "discovery.json",
            {
                "rows": rows,
                "prime_selectors": selectors,
                "availability": _availability_evidence(availability),
                "worker_preprobe_candidates": sorted(preprobe_ids),
                "allowed_route_prefixes": sorted(task.allowed_route_prefixes),
                "excluded_route_ids": sorted(task.excluded_route_ids),
                "prime_visibility": {
                    "source": visibility.source,
                    "timestamp": visibility.timestamp,
                    "count": visibility.count,
                    "digest": visibility.digest,
                    "refresh_failure": visibility.failure.to_dict() if visibility.failure else None,
                },
            },
        )
        # Canonical live admission stays authoritative; #626's availability and
        # task policy narrow the pool further (intersection = fail closed).
        admitted = await asyncio.to_thread(_worker_admission, config, rows)
        admitted.write_receipt(directory / "admission.json")
        if not candidates:
            raise RuntimeError(
                "no worker route passed canonical availability, Prime-visibility, "
                "route-policy, and task gates"
            )

        probe_base = transport.base_url.rstrip("/") + "/v1"
        controller = WorkerController(
            task,
            inventory_rows=narrowed_rows,
            prime_selectors=selectors,
            probe=openai_health_probe(probe_base, api_key=api_key),
            adapter=PrimeFileAdapter(directory),
            cache=HealthCache(),
            budget=RuntimeBudget(**config.get("budget", {})),
            emit=emit,
            admitted=admitted,
            require_admission=True,
            run_dir=directory,
        )
        try:
            outcome = await controller.run(config["prompt"])
        finally:
            # Persist confirmation results (source + observed_at) with the set.
            if controller.admitted is not None:
                controller.admitted.write_receipt(directory / "admission.json")
    except Exception as exc:
        outcome = WorkerOutcome(
            "FAIL_CLOSED",
            "",
            f"discovery/runtime unavailable: {exc}; inspect Prime registry and OmniRoute",
            None,
            None,
            (),
        )
        emit({"event": "final", "state": outcome.state, "diagnostic": outcome.diagnostic})
    try:
        atomic_json(directory / "outcome.json", asdict(outcome))
    except Exception as exc:
        outcome = WorkerOutcome(
            "FAIL_CLOSED",
            "",
            f"outcome publication failed: {exc}; restore artifact directory write access",
            None,
            None,
            outcome.attempts,
        )
    try:
        with events.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "event": "final",
                        "state": outcome.state,
                        "diagnostic": outcome.diagnostic,
                        "spawn_id": outcome.spawn_id,
                        "final_successful_model": outcome.candidate.selector
                        if outcome.candidate
                        else None,
                    }
                )
                + "\n"
            )
    except OSError:
        pass  # The explicit stdout outcome is still mandatory when disk access fails.
    print(outcome.render(), flush=True)
    return 0 if outcome.state == "SUCCESS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--sync-visibility",
        action="store_true",
        help="explicitly rewrite the Prime model registry before launch (default: read-only)",
    )
    args = parser.parse_args()
    return asyncio.run(cli_run(args.directory, sync_visibility=args.sync_visibility))


if __name__ == "__main__":
    raise SystemExit(main())
