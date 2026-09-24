"""Owned worker attempts: admission -> terminal validation -> replacement.

The CLI runs in Verdict's environment. Prime's small kernel bridge supplies only
native spawn/collect/delete RPCs; provider errors never unwind the controller.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from verdict.subagent_selection import (
    HealthCache,
    HealthResult,
    LaunchCandidate,
    WorkerTask,
    WorkerTerminal,
    _failure_cooldown,
    classify_worker_failure,
    eligible_worker_candidates,
    fetch_omniroute_inventory,
    openai_health_probe,
)


@dataclass(frozen=True)
class RuntimeBudget:
    # Unique candidates are the default attempt bound; wall time includes probes.
    total_seconds: float = 900
    attempt_seconds: float = 180
    probe_seconds: float = 15
    cleanup_seconds: float = 30
    max_attempts: int | None = None

    def __post_init__(self) -> None:
        if min(self.total_seconds, self.attempt_seconds, self.probe_seconds,
               self.cleanup_seconds) <= 0:
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
            return f"SUCCESS model={self.candidate.selector} spawn_id={self.spawn_id}\n{self.output}"
        return f"FAIL_CLOSED {self.diagnostic}"


class WorkerAdapter(Protocol):
    async def spawn(self, prompt: str, *, name: str, model: str) -> Mapping[str, Any]: ...
    async def collect(self, handle: Mapping[str, Any]) -> WorkerTerminal | None: ...
    async def delete(self, handle: Mapping[str, Any]) -> None: ...


class AttemptFailureError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


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


class WorkerController:
    """One immutable task, one owned child at a time, exactly one final outcome."""

    def __init__(
        self, task: WorkerTask, *, inventory_rows: Iterable[Mapping[str, Any]],
        prime_selectors: Iterable[str], probe: Callable[[LaunchCandidate], HealthResult],
        adapter: WorkerAdapter, cache: HealthCache, budget: RuntimeBudget = RuntimeBudget(),
        now: Callable[[], datetime] | None = None,
        emit: Callable[[dict[str, Any]], None] | None = None,
        validator: Callable[[str], bool] | None = None,
    ) -> None:
        self.candidates = eligible_worker_candidates(task, inventory_rows, prime_selectors)
        self.probe, self.adapter, self.cache, self.budget = probe, adapter, cache, budget
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.emit = emit or (lambda event: None)
        self.validator = validator or (lambda output: bool(output.strip()))
        self.attempts: list[tuple[str, str]] = []
        self.events: list[dict[str, Any]] = []
        self.outcome: WorkerOutcome | None = None
        self.operation_id = uuid.uuid4().hex

    def event(self, event: str, **fields: Any) -> None:
        row = {"event": event, "operation_id": self.operation_id, **fields}
        self.events.append(row)
        self.emit(row)

    def finish(self, state: str, diagnostic: str = "", *, output: str = "",
               candidate: LaunchCandidate | None = None, spawn_id: str | None = None) -> WorkerOutcome:
        if self.outcome is None:
            self.outcome = WorkerOutcome(state, output, diagnostic, candidate, spawn_id,
                                         tuple(self.attempts))
            self.event("final", state=state, diagnostic=diagnostic, spawn_id=spawn_id,
                       final_successful_model=candidate.selector if candidate else None)
        return self.outcome

    async def run(self, prompt: str) -> WorkerOutcome:
        if self.outcome is not None:
            return self.outcome
        try:
            return await self._run(prompt)
        except Exception as exc:
            return self.finish("FAIL_CLOSED", f"controller infrastructure: {type(exc).__name__}: {exc}; "
                               "inspect events and restore registry/cache/bridge access")

    async def _run(self, prompt: str) -> WorkerOutcome:
        deadline = time.monotonic() + self.budget.total_seconds
        previous: str | None = None
        for candidate in self.candidates:
            if time.monotonic() >= deadline:
                return self.finish("FAIL_CLOSED", "total time budget reached; inspect attempt events; "
                                   "increase total_seconds or restore provider capacity")
            if self.budget.max_attempts is not None and len(self.attempts) >= self.budget.max_attempts:
                return self.finish("FAIL_CLOSED", "replacement budget exhausted; increase max_attempts "
                                   "or restore provider capacity")
            health = self.cache.usable(candidate.selector, now=self.now())
            if health is None:
                try:
                    health = await asyncio.wait_for(asyncio.to_thread(self.probe, candidate),
                        min(self.budget.probe_seconds, deadline - time.monotonic()))
                    if not isinstance(health, HealthResult):
                        raise AttemptFailureError("malformed_probe")
                except Exception as exc:
                    health = self.failure(exc)
                if health.healthy:
                    self.cache.record(candidate.selector, health, now=self.now())
                else:
                    self.cache.record_failure(candidate, health, now=self.now())
            self.event("health", model=candidate.selector, provider=candidate.route_id.split("/")[0],
                       classification=health.category, eligible=health.healthy)
            if not health.healthy:
                self.event("exclusion", model=candidate.selector, classification=health.category,
                           cooldown_seconds=_failure_cooldown(health), replacement=True)
                previous = candidate.selector
                continue
            number = len(self.attempts) + 1
            self.event("selection", attempt=number, model=candidate.selector,
                       provider=candidate.route_id.split("/")[0], previous_model=previous,
                       replacement_model=candidate.selector if previous else None)
            handle: Mapping[str, Any] | None = None
            spawn_id: str | None = None
            name = f"verdict-{self.operation_id[:10]}-{number}"
            admission_pending = True
            try:
                attempt_deadline = min(deadline, time.monotonic() + self.budget.attempt_seconds)
                handle = await asyncio.wait_for(self.adapter.spawn(
                    prompt, name=name,
                    model=candidate.selector), max(0.001, attempt_deadline - time.monotonic()))
                admission_pending = False
                spawn_id = handle.get("rlm_child_id")
                if not isinstance(spawn_id, str) or not spawn_id or handle.get("model") != candidate.selector:
                    raise AttemptFailureError("malformed_admission")
                self.event("admission", attempt=number, model=candidate.selector,
                           spawn_id=spawn_id, admitted=True)
                while True:
                    remaining = attempt_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("child execution deadline")
                    terminal = await asyncio.wait_for(self.adapter.collect(handle), remaining)
                    if terminal is not None:
                        self.event("terminal", attempt=number, model=candidate.selector,
                                   spawn_id=spawn_id, child_state=getattr(terminal, "state", "malformed"),
                                   replied=getattr(terminal, "replied", False))
                        output = validate_terminal(terminal)
                        if not self.validator(output):
                            raise AttemptFailureError("invalid_output")
                        break
                    await asyncio.sleep(min(0.1, remaining))
            except Exception as exc:
                if admission_pending and isinstance(exc, TimeoutError):
                    # Spawn may have been admitted after the RPC deadline. Reap by
                    # our unique name before permitting another writer.
                    handle = {"rlm_child_id": name, "model": candidate.selector}
                health = self.failure(exc)
                self.attempts.append((candidate.selector, health.category))
                self.cache.record_failure(candidate, health, now=self.now())
                self.event("failure", attempt=number, model=candidate.selector, spawn_id=spawn_id,
                           classification=health.category, cooldown_seconds=_failure_cooldown(health),
                           excluded=True, replacement=True)
                if handle is not None:
                    # A timed-out writer must be reaped before a replacement can write.
                    try:
                        await asyncio.wait_for(self.adapter.delete(handle), self.budget.cleanup_seconds)
                    except Exception as cleanup:
                        return self.finish("FAIL_CLOSED", f"cleanup_unconfirmed spawn={spawn_id}: "
                            f"{cleanup}; stop this owned child before retrying")
                previous = candidate.selector
                continue
            self.attempts.append((candidate.selector, "completed"))
            self.cache.record(candidate.selector, HealthResult(True, "healthy"), now=self.now())
            return self.finish("SUCCESS", output=output, candidate=candidate, spawn_id=spawn_id)
        detail = ", ".join(f"{model}:{category}" for model, category in self.attempts)
        return self.finish("FAIL_CLOSED", "eligible candidates exhausted (including active cooldowns); "
                           f"restore provider health or refresh discovery; attempts={detail}")

    @staticmethod
    def failure(exc: Exception) -> HealthResult:
        if isinstance(exc, AttemptFailureError):
            return HealthResult(False, exc.category)
        return classify_worker_failure(exc)


class CallbackAdapter:
    """Compatibility adapter for terminal-producing callbacks, never spawn callbacks."""
    def __init__(self, execute: Callable[[str], Awaitable[WorkerTerminal]]) -> None:
        self.execute = execute

    async def spawn(self, prompt: str, *, name: str, model: str) -> Mapping[str, Any]:
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

    async def spawn(self, prompt: str, *, name: str, model: str) -> Mapping[str, Any]:
        value = await self.rpc("spawn", prompt=prompt, name=name, model=model)
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
        messages = [json.loads(line).get("message", {}) for line in files[0].read_text().splitlines()]
        assistants = [message for message in messages if message.get("role") == "assistant"]
        if not assistants:
            return WorkerTerminal(str(row.get("status")), replied=row.get("replied_since_task") is True)
        last = assistants[-1]
        expected = handle.get("model")
        if expected is not None and not last.get("errorMessage"):
            actual = f"{last.get('provider')}/{last.get('model')}"
            if actual != expected:
                raise AttemptFailureError("model_provenance_mismatch")
        output = "\n".join(block["text"] for block in last.get("content", [])
                           if isinstance(block, dict) and block.get("type") == "text"
                           and isinstance(block.get("text"), str))
        return WorkerTerminal(str(row.get("status")), output,
                              row.get("replied_since_task") is True,
                              last.get("errorMessage"), last.get("stopReason"))

    async def delete(self, handle: Mapping[str, Any]) -> None:
        await self.rpc("delete", child_id=handle["rlm_child_id"])


async def cli_run(directory: Path) -> int:
    config = json.loads((directory / "config.json").read_text())
    events = directory / "events.jsonl"
    def emit(event: dict[str, Any]) -> None:
        if event["event"] == "final":
            return  # Published exactly once below, after serializing the outcome.
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event) + "\n")
    try:
        # CLI registry listing is complete; find_models has a bounded search limit.
        process = await asyncio.create_subprocess_exec("prime-agent", "model", "list",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError("Prime model registry unavailable: " + stderr.decode()[:500])
        selectors = ["/".join(line.split()[:2]) for line in stdout.decode().splitlines()[1:]
                     if len(line.split()) >= 2]
        rows = await asyncio.to_thread(fetch_omniroute_inventory)
        atomic_json(directory / "discovery.json", {"rows": rows, "prime_selectors": selectors})
        task_config = config.get("task", {})
        task_config["required_capabilities"] = frozenset(task_config.get("required_capabilities", []))
        controller = WorkerController(WorkerTask(**task_config), inventory_rows=rows,
            prime_selectors=selectors, probe=openai_health_probe(),
            adapter=PrimeFileAdapter(directory), cache=HealthCache(),
            budget=RuntimeBudget(**config.get("budget", {})), emit=emit)
        outcome = await controller.run(config["prompt"])
    except Exception as exc:
        outcome = WorkerOutcome("FAIL_CLOSED", "", f"discovery/runtime unavailable: {exc}; "
                                "inspect Prime registry and OmniRoute", None, None, ())
        emit({"event": "final", "state": outcome.state, "diagnostic": outcome.diagnostic})
    try:
        atomic_json(directory / "outcome.json", asdict(outcome))
    except Exception as exc:
        outcome = WorkerOutcome("FAIL_CLOSED", "", f"outcome publication failed: {exc}; "
                                "restore artifact directory write access", None, None, outcome.attempts)
    try:
        with events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "final", "state": outcome.state,
                "diagnostic": outcome.diagnostic, "spawn_id": outcome.spawn_id,
                "final_successful_model": outcome.candidate.selector if outcome.candidate else None}) + "\n")
    except OSError:
        pass  # The explicit stdout outcome is still mandatory when disk access fails.
    print(outcome.render(), flush=True)
    return 0 if outcome.state == "SUCCESS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    return asyncio.run(cli_run(args.directory))


if __name__ == "__main__":
    raise SystemExit(main())
