"""Read-only verified snapshots and shared wait-before-render consumer wiring.

The action itself never refreshes. CLI/TUI consumers opt into the separate
bounded coordinator; autocomplete uses local_only and never waits.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import select
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from verdict.actions.base import ActionResult
from verdict.admission import RuntimeEvidence, RuntimeObservation, active_controller_route, admit
from verdict.free_tier_admit import normalize_omniroute_origin
from verdict.orchestration.health_cache import HealthCache
from verdict.orchestration.verified_models import (
    EvidenceSnapshots,
    VerifiedModelQuery,
    VerifiedModelsView,
    project_verified_models,
    snapshots_from_documents,
)
from verdict.orchestration.verified_refresh import (
    RefreshConfig,
    RefreshOutcome,
    RefreshSnapshot,
    RowInput,
    config_from_env,
    refresh_for_consumer,
    select_candidates,
)
from verdict.tui_verified_controls import VerifiedControlsController


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class StorePaths:
    health_cache: Path
    ladder: Path
    worker: Path
    receipt: Path
    catalog: Path

    @classmethod
    def defaults(cls, state_dir: Path | None = None) -> StorePaths:
        base = state_dir or Path(os.environ.get("VERDICT_HOME", Path.home() / ".verdict"))
        return cls(
            Path(os.environ.get("VERDICT_HEALTH_CACHE", str(base / "health-cache.json"))),
            base / "orchestration-health.json",
            base / "subagent-health.json",
            base / "admission-latest.json",
            base / "verified-models-snapshot.json",
        )


def _read_document(path: Path, source: str, errors: list[str]) -> Mapping[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        errors.append(f"{source}: unreadable or invalid JSON")
        return None
    if not isinstance(document, Mapping):
        errors.append(f"{source}: expected an object")
        return None
    return document


def _runtime_negatives(evidence: EvidenceSnapshots, now: datetime) -> RuntimeEvidence:
    """Only validated negatives participate in current, unscoped admission."""
    observations: list[RuntimeObservation] = []

    def add(key: str, category: str, source: str, until: datetime) -> None:
        if until <= now:
            return
        state = (
            "unauthorized"
            if category in {"authentication", "permission"}
            else "exhausted"
            if category in {"payment_required", "rate_limited", "quota_exhausted"}
            else "unhealthy"
        )
        observations.append(
            RuntimeObservation(key, state, category, source, until=until.isoformat())
        )

    for row in evidence.at_rest.values():
        if row.healthy:
            # Timestamp-only supersession hint: never a healthy admission state.
            observations.append(
                RuntimeObservation(
                    f"route:{row.route_id}",
                    "success_hint",
                    "ok",
                    "health_cache",
                    observed_at=(row.last_success_at or row.checked_at).isoformat(),
                )
            )
        if not row.healthy:
            add(f"route:{row.route_id}", row.category, "health_cache", row.until)
    for ladder_row in evidence.ladder_health.values():
        if not ladder_row.healthy:
            add(
                f"route:{ladder_row.route_id}",
                ladder_row.category,
                "ladder_state",
                ladder_row.checked_at + timedelta(seconds=300),
            )
    for worker_row in evidence.worker_health.values():
        if not worker_row.healthy:
            add(
                f"route:{worker_row.route_id}",
                worker_row.category,
                "worker_health",
                worker_row.expires_at,
            )
    for cooldown in (
        *evidence.at_rest_cooldowns,
        *evidence.ladder_cooldowns,
        *evidence.worker_cooldowns,
    ):
        observations.append(
            RuntimeObservation(
                f"{cooldown.scope}:{cooldown.name}",
                "cooldown",
                cooldown.category,
                cooldown.source,
                until=cooldown.until.isoformat(),
                pool_id=cooldown.pool_id,
                account_id=cooldown.account_id,
            )
        )
    return RuntimeEvidence(tuple(observations), ("verified_snapshot_negatives",))


@dataclass
class VerifiedSnapshotAdapter:
    """Metadata is loaded once; only local evidence is reloaded after waiting.

    Supply rows/connections or read-only cached catalog paths for offline use.
    No selector, receipt writer or harness registry refresh is called here.
    """

    gateway: str
    paths: StorePaths
    inventory_rows: Sequence[Mapping[str, Any]] | None = None
    connections: Sequence[Mapping[str, Any]] | None = None
    local_only: bool = False
    clock: Callable[[], datetime] = utc_now
    monotonic: Callable[[], float] = time.monotonic
    deadline: float | None = None
    policy_exclusions: Mapping[str, str] | None = None
    visibility_unavailable: Sequence[str] | None = None
    _metadata_loaded: bool = False
    _metadata_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self.gateway = normalize_omniroute_origin(self.gateway)

    def _timeout(self) -> float:
        remaining = 15.0 if self.deadline is None else self.deadline - self.monotonic()
        if remaining <= 0:
            raise ValueError("gateway metadata wait reached wall_cap")
        return min(15.0, remaining)

    def _load_metadata(self) -> None:
        if self._metadata_loaded:
            return
        errors: list[str] = []
        if self.local_only:
            catalog = _read_document(self.paths.catalog, "inventory", errors) or {}
            if self.inventory_rows is None:
                rows = catalog.get("inventory_rows", catalog.get("data", []))
                self.inventory_rows = (
                    [r for r in rows if isinstance(r, Mapping)] if isinstance(rows, list) else []
                )
            if self.connections is None:
                connections = catalog.get("connections")
                self.connections = (
                    [r for r in connections if isinstance(r, Mapping)]
                    if isinstance(connections, list)
                    else None
                )
        else:
            from verdict.orchestration.run import (
                fetch_connections,
                fetch_inventory,
                resolve_api_key,
            )

            key = (
                resolve_api_key()
                if self.inventory_rows is None or self.connections is None
                else None
            )
            for endpoint, missing in (
                ("/v1/models", self.inventory_rows is None),
                ("/api/providers", self.connections is None),
            ):
                if not missing:
                    continue
                try:
                    if endpoint == "/v1/models":
                        self.inventory_rows = fetch_inventory(
                            self.gateway, api_key=key, timeout=self._timeout()
                        )
                    else:
                        self.connections = fetch_connections(
                            self.gateway, api_key=key, timeout=self._timeout()
                        )
                except Exception:
                    # Never expose a URL, credential, response body or exception text.
                    raise ValueError(f"gateway {endpoint} metadata unavailable") from None
        self._metadata_errors = tuple(errors)
        self._metadata_loaded = True

    def load(self, query: VerifiedModelQuery) -> VerifiedModelsView:
        now = self.clock()
        # Invoke the public pure validator BEFORE disk/network I/O.
        project_verified_models([], None, EvidenceSnapshots(), now=now, query=query)
        self._load_metadata()
        errors = list(self._metadata_errors)
        documents: dict[str, Any] = {
            "health_cache_doc": _read_document(self.paths.health_cache, "health_cache", errors),
            "ladder_state_doc": _read_document(self.paths.ladder, "ladder_state", errors),
            "worker_health_doc": _read_document(self.paths.worker, "worker_health", errors),
            "admission_receipt_doc": _read_document(self.paths.receipt, "admission", errors),
        }
        evidence = snapshots_from_documents(**documents, now=now, extra_source_errors=errors)
        exclusions = dict(self.policy_exclusions or {})
        controller = active_controller_route()
        if controller:
            exclusions[controller] = "active_controller"
        facts: dict[str, Mapping[str, Any]] | None = None
        if self.connections is not None:
            admitted = admit(
                self.inventory_rows or [],
                self.connections,
                _runtime_negatives(evidence, now),
                now=now,
                require_runtime=False,
            )
            facts = {record.route_id: record.to_dict() for record in admitted.records}
        evidence = snapshots_from_documents(
            **documents,
            now=now,
            admission_facts=facts,
            policy_exclusions=exclusions,
            visibility_unavailable=self.visibility_unavailable,
            extra_source_errors=errors,
        )
        return project_verified_models(
            self.inventory_rows or [], self.connections, evidence, now=now, query=query
        )

    def refresh_snapshot(self, view: VerifiedModelsView) -> RefreshSnapshot:
        # Generation binds evidence, not the wall clock or presentation query.
        digest = hashlib.sha256()
        for path in (self.paths.health_cache, self.paths.ladder, self.paths.worker):
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"missing")
        rows = tuple(
            RowInput(
                route_id=row.route_id,
                provider=row.provider,
                status=row.status.value,
                capacity_class=row.capacity_class or "unknown",
                refreshable=row.refreshable,
                refresh_reason=row.refresh_reason,
                last_success_at=row.last_success_at,
            )
            for row in view.rows
        )
        return RefreshSnapshot(rows, generation=digest.hexdigest(), gateway_origin=self.gateway)


def action_models_verified(**kwargs: Any) -> ActionResult:
    """Shared read action. Never starts or joins a refresh, even with stale proof."""
    query = kwargs.get("query") or VerifiedModelQuery(
        status=kwargs.get("status"),
        provider=kwargs.get("provider"),
        search=kwargs.get("search"),
        page=kwargs.get("page", 1),
        page_size=kwargs.get("page_size", 50),
    )
    adapter = kwargs.get("adapter") or VerifiedSnapshotAdapter(
        gateway=kwargs.get("gateway", os.environ.get("VERDICT_GATEWAY", "http://127.0.0.1:20128")),
        paths=kwargs.get("paths") or StorePaths.defaults(kwargs.get("state_dir")),
        inventory_rows=kwargs.get("inventory_rows"),
        connections=kwargs.get("connections"),
        local_only=bool(kwargs.get("local_only")),
        clock=kwargs.get("clock") or utc_now,
        policy_exclusions=kwargs.get("policy_exclusions"),
        visibility_unavailable=kwargs.get("visibility_unavailable"),
    )
    try:
        return ActionResult(data=adapter.load(query).to_dict())
    except ValueError as exc:
        message = str(exc)
        if message not in {
            "gateway /v1/models metadata unavailable",
            "gateway /api/providers metadata unavailable",
            "gateway metadata wait reached wall_cap",
        }:
            message = "invalid verified model filters, paging or snapshot"
        return ActionResult(data={"error": message}, ok=False, exit_code=2)


def _summary(outcome: RefreshOutcome) -> dict[str, Any]:
    return {
        "outcome": outcome.outcome,
        "job_id": outcome.job_id,
        "probed": outcome.probed,
        "healthy": outcome.verified + getattr(outcome, "alive", 0),
        "verified": outcome.verified,
        "alive": getattr(outcome, "alive", 0),
        "failed": outcome.failed,
        "unavailable": outcome.unavailable,
        "requests_made": outcome.requests_made,
        "elapsed_seconds": outcome.elapsed_seconds,
        "complete": outcome.complete,
        "cap_reason": outcome.cap_reason,
        "last_known": outcome.outcome in {"cancelled", "auto_refresh_disabled"},
    }


def _lazy_live_transport(gateway: str) -> Callable[[str, str, float], Any]:
    """Construct live transport on the first permitted dispatch, never at plan time."""
    transport: Callable[[str, str, float], Any] | None = None
    lock = threading.Lock()

    def call(route_id: str, phase: str, timeout: float) -> Any:
        nonlocal transport
        with lock:
            if transport is None:
                from verdict.orchestration.run import resolve_api_key
                from verdict.prove_at_rest import live_transport

                transport = live_transport(gateway, api_key=resolve_api_key())
        return transport(route_id, phase, timeout)

    return call


def _wait_on_worker(operation: Callable[..., Any], *, live: bool = False) -> Callable[..., Any]:
    """Drain bounded coordinator events on the UI thread, with shared cancellation.

    WaitRunner owns the call contract. This driver supplies the responsive live
    execution boundary: workers never write presentation; Esc/Ctrl-C sets the
    cancellation callback that the coordinator shares with joined consumers.
    """

    def run(snapshot: RefreshSnapshot, **kwargs: Any) -> Any:
        events: queue.Queue[tuple[str, Any]] = queue.Queue()
        stopped = threading.Event()
        external_cancel = kwargs.pop("cancel", None)
        on_progress = kwargs.pop("on_progress", None)
        config: RefreshConfig = kwargs["config"]

        def cancelled() -> bool:
            if external_cancel is not None and external_cancel():
                stopped.set()
            return stopped.is_set()

        def worker() -> None:
            try:
                result = operation(
                    snapshot,
                    **kwargs,
                    cancel=cancelled,
                    on_progress=lambda event: events.put(("progress", event)),
                )
                events.put(("done", result))
            except BaseException as exc:
                events.put(("error", exc))

        thread = threading.Thread(target=worker, name="verified-model-refresh", daemon=True)
        previous: Any = None
        installed = live and threading.current_thread() is threading.main_thread()
        if installed:
            previous = signal.signal(signal.SIGINT, lambda _signum, _frame: stopped.set())
        terminal_mode: Any = None
        if live and sys.stdin.isatty():
            try:
                import termios
                import tty

                terminal_mode = termios.tcgetattr(sys.stdin.fileno())
                tty.setcbreak(sys.stdin.fileno())
            except (ImportError, OSError, ValueError):
                terminal_mode = None
        from verdict.orchestration.verified_refresh import ProgressEvent

        selected = select_candidates(
            snapshot,
            needed_ids=kwargs.get("needed_ids", []),
            explicit=bool(kwargs.get("explicit")),
            config=config,
            authorized=bool(kwargs.get("authorized")),
        )
        latest = ProgressEvent(0, "", 0, len(selected), 0, 0, 0, 0, 0, 0.0, {}, None)
        wait_started = time.monotonic()
        last_tick = wait_started
        if on_progress is not None:
            on_progress(latest)
        thread.start()
        try:
            # Finite upper bound includes one in-flight timeout after cancellation.
            for _tick in range(int((config.wall_seconds + 16.0) / 0.05) + 1):
                cancelled()
                if live and sys.stdin.isatty():
                    try:
                        if select.select([sys.stdin], [], [], 0)[0] and sys.stdin.read(1) in {
                            "\x1b",
                            "\x03",
                        }:
                            stopped.set()
                    except (OSError, ValueError):
                        pass
                try:
                    kind, value = events.get(timeout=0.05)
                except queue.Empty:
                    elapsed = time.monotonic() - wait_started
                    if on_progress is not None and time.monotonic() - last_tick >= 1.0:
                        on_progress(replace(latest, elapsed_seconds=elapsed))
                        last_tick = time.monotonic()
                    continue
                if kind == "progress":
                    if isinstance(value, ProgressEvent):
                        latest = value
                    if on_progress is not None:
                        on_progress(value)
                    last_tick = time.monotonic()
                elif kind == "error":
                    raise ValueError(
                        "bounded refresh failed; last-known evidence remains available"
                    ) from None
                else:
                    return value
            stopped.set()
            raise ValueError("bounded refresh exceeded its deadline")
        finally:
            if terminal_mode is not None:
                import termios

                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal_mode)
            if installed:
                signal.signal(signal.SIGINT, previous)

    return run


def consume_verified_models(
    *,
    gateway: str,
    query: VerifiedModelQuery = VerifiedModelQuery(),
    adapter: VerifiedSnapshotAdapter | None = None,
    paths: StorePaths | None = None,
    config: RefreshConfig | None = None,
    no_refresh: bool = False,
    manual: bool = False,
    read_line: Callable[[str], Any] = input,
    write: Callable[[str], None] = lambda _s: None,
    cancel: Callable[[], bool] | None = None,
    transport: Callable[[str, str, float], Any] | None = None,
    run_refresh: Callable[..., Any] = refresh_for_consumer,
    live: bool = False,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    on_projection: Callable[[Mapping[str, Any], VerifiedSnapshotAdapter], None] | None = None,
) -> ActionResult:
    """Snapshot, wait, reload, final projection, then optional convenience callback.

    ``on_projection`` observes exactly the final render payload and actual adapter,
    never initial/provisional rows. Its failure adds only a sanitized warning;
    verification and presentation remain valid. The default has no extra I/O.
    """
    from verdict.actions.registry import run_action
    from verdict.orchestration.verified_models_render import (
        format_verified_progress,
        render_refresh_plan,
    )

    config = config or config_from_env()
    started = monotonic()
    adapter = adapter or VerifiedSnapshotAdapter(
        gateway,
        paths or StorePaths.defaults(),
        clock=clock,
        monotonic=monotonic,
        deadline=started + config.wall_seconds,
    )
    try:
        initial = adapter.load(query)
    except ValueError as exc:
        message = str(exc)
        if message not in {
            "gateway /v1/models metadata unavailable",
            "gateway /api/providers metadata unavailable",
            "gateway metadata wait reached wall_cap",
        }:
            message = "invalid verified model filters, paging or snapshot"
        return ActionResult(data={"error": message}, ok=False, exit_code=2)
    snapshot = adapter.refresh_snapshot(initial)
    needed_ids = [row.route_id for row in snapshot.rows]
    summary: dict[str, Any] = {"outcome": "no_refresh", "last_known": True, "requests_made": 0}
    outcome: RefreshOutcome | None = None
    controller = VerifiedControlsController(
        read_line, write, _wait_on_worker(run_refresh, live=live), is_cancelled=cancel
    )
    if manual:
        caps = {
            "max_routes": config.max_routes,
            "max_requests": config.max_requests,
            "wall_seconds": config.wall_seconds,
            "concurrency": config.concurrency,
        }
        plan = run_action(
            "models.refresh.plan",
            {
                "snapshot_rows": initial.to_dict()["rows"],
                "needed_ids": needed_ids,
                "consumer": "verified_view",
                "gateway_origin": adapter.gateway,
                "evidence_generation": snapshot.generation,
                "filters": dict(initial.filters),
                "page": initial.page,
                "caps": caps,
                "now": clock(),
                "include_metered": True,
            },
        )
        if not plan.ok:
            return plan
        write(render_refresh_plan(plan.data))
        consent = controller.confirm("Execute this exact refresh plan?")
        if not consent.granted:
            summary = {
                "outcome": "cancelled" if consent.cancelled else "not_confirmed",
                "last_known": True,
                "requests_made": 0,
            }
        else:
            current = adapter.refresh_snapshot(adapter.load(query))

            def execute(current_snapshot: RefreshSnapshot, **extra: Any) -> ActionResult:
                return run_action(
                    "models.refresh.execute",
                    {
                        "confirmed": True,
                        "plan": plan.data,
                        "snapshot": current_snapshot,
                        "cache": HealthCache(adapter.paths.health_cache),
                        "transport": transport
                        or (_lazy_live_transport(adapter.gateway) if live else None),
                        "clock": clock,
                        "monotonic": monotonic,
                        "now": clock(),
                        "on_progress": extra.get("on_progress"),
                        "cancel": extra.get("cancel"),
                    },
                )

            manual_controller = replace(controller, run_refresh=_wait_on_worker(execute, live=live))
            try:
                execution = manual_controller.refresh(
                    current,
                    consumer="verified_view",
                    needed_ids=needed_ids,
                    config=config,
                    authorized=True,
                    on_progress=lambda event: write(format_verified_progress(event)),
                )
                summary = dict(execution.data)
                summary["last_known"] = not execution.ok or summary.get("outcome") == "cancelled"
            except (ValueError, RuntimeError):
                summary = {
                    "outcome": "error",
                    "last_known": True,
                    "requests_made": None,
                    "reason": "refresh_failed",
                }
    elif not no_refresh:
        if not config.auto_refresh:
            summary = {"outcome": "auto_refresh_disabled", "last_known": True, "requests_made": 0}
        elif all(row.status == "VERIFIED" for row in snapshot.rows):
            summary = {"outcome": "reused_fresh", "last_known": False, "requests_made": 0}
        elif not select_candidates(snapshot, needed_ids=needed_ids, explicit=False, config=config):
            summary = {"outcome": "nothing_eligible", "last_known": True, "requests_made": 0}
        else:
            remaining = config.wall_seconds - (monotonic() - started)
            if remaining <= 0:
                summary = {
                    "outcome": "capped",
                    "cap_reason": "wall_cap",
                    "last_known": True,
                    "requests_made": 0,
                }
            else:
                try:
                    outcome = controller.refresh(
                        snapshot,
                        consumer="verified_view",
                        needed_ids=needed_ids,
                        config=replace(config, wall_seconds=remaining),
                        cache=HealthCache(adapter.paths.health_cache),
                        transport=transport
                        or (_lazy_live_transport(adapter.gateway) if live else None),
                        clock=clock,
                        monotonic=monotonic,
                        on_progress=lambda event: write(format_verified_progress(event)),
                    )
                    summary = _summary(outcome)
                except (ValueError, RuntimeError):
                    summary = {
                        "outcome": "error",
                        "last_known": True,
                        "requests_made": None,
                        "reason": "refresh_failed",
                    }
    final_result = run_action("models.verified", {"adapter": adapter, "query": query})
    if not final_result.ok:
        return final_result
    final = final_result.data
    if outcome is not None:
        for row in final["rows"]:
            route = outcome.route_outcomes.get(row["route_id"])
            if route is not None and route.refresh_reason:
                row["refresh_reason"] = route.refresh_reason
    manual_outcomes = summary.get("route_outcomes")
    if isinstance(manual_outcomes, Mapping):
        # Unit3 returns exact planned ids. Only projection-safe public ids may
        # appear in the final consumer metadata; unknown/withheld ids stay private.
        visible_ids = {row["route_id"] for row in final["rows"]}
        summary["route_outcomes"] = {
            rid: details for rid, details in manual_outcomes.items() if rid in visible_ids
        }
        for row in final["rows"]:
            details = summary["route_outcomes"].get(row["route_id"])
            if isinstance(details, Mapping) and details.get("refresh_reason"):
                row["refresh_reason"] = details["refresh_reason"]
    if summary.get("cap_reason") == "wall_cap" and outcome is None:
        for row in final["rows"]:
            if row["refreshable"]:
                row["refresh_reason"] = "wall_cap"
    final["refresh"] = summary
    if on_projection is not None:
        try:
            on_projection(final, adapter)
        except Exception:
            # Convenience callbacks must not leak provider/file exception text
            # or invalidate evidence, admission, consent, or final presentation.
            final["completion_warning"] = "post-projection callback failed; evidence unchanged"
    return ActionResult(data=final)


def selection_refresh_hook(
    gateway: str,
    *,
    state_dir: Path | None = None,
    adapter: VerifiedSnapshotAdapter | None = None,
    transport: Callable[[str, str, float], Any] | None = None,
    config: RefreshConfig | None = None,
    run_refresh: Callable[..., Any] = refresh_for_consumer,
) -> Callable[[Sequence[str], datetime], Mapping[str, str] | None] | None:
    """Live selection hook factory. Runtime still reconfirms the selected route."""
    config = config or config_from_env()
    if not config.auto_refresh:
        return None

    def hook(ids: Sequence[str], now: datetime) -> Mapping[str, str] | None:
        started = time.monotonic()
        current_adapter = adapter or VerifiedSnapshotAdapter(
            gateway,
            StorePaths.defaults(state_dir),
            clock=lambda: now,
            deadline=started + config.wall_seconds,
        )
        current_adapter._load_metadata()
        from verdict.admission import canonical_route_id

        wanted = {canonical_route_id(rid) for rid in ids}
        metadata = [
            r
            for r in current_adapter.inventory_rows or []
            if canonical_route_id(str(r.get("id", ""))) in wanted
        ]
        scoped = replace(current_adapter, inventory_rows=metadata, clock=lambda: now)
        rows: list[RowInput] = []
        for page in range(1, (len(metadata) + 199) // 200 + 1):
            view = scoped.load(VerifiedModelQuery(page=page, page_size=200))
            rows.extend(scoped.refresh_snapshot(view).rows)
        snapshot = RefreshSnapshot(tuple(rows), gateway_origin=scoped.gateway)
        remaining = config.wall_seconds - (time.monotonic() - started)
        if remaining <= 0:
            return None
        blocked: dict[str, str] = {}
        if select_candidates(snapshot, needed_ids=ids, explicit=True, config=config):
            controller = VerifiedControlsController(
                read_line=lambda _p: None,
                write=lambda line: print(line, file=sys.stderr),
                run_refresh=_wait_on_worker(run_refresh, live=transport is None),
            )
            from verdict.orchestration.verified_models_render import format_verified_progress

            outcome = controller.refresh(
                snapshot,
                consumer="selection",
                needed_ids=ids,
                config=replace(config, wall_seconds=remaining),
                explicit=True,
                cache=HealthCache(scoped.paths.health_cache),
                transport=transport or _lazy_live_transport(scoped.gateway),
                clock=lambda: now,
                on_progress=lambda event: print(format_verified_progress(event), file=sys.stderr),
            )
            for rid, route_outcome in outcome.route_outcomes.items():
                if rid not in wanted:
                    continue
                if (
                    route_outcome.probed
                    and not route_outcome.verified
                    and not getattr(route_outcome, "alive", False)
                ):
                    blocked[rid] = "refresh_failed"
                elif route_outcome.refresh_reason == "provider_scope_stopped":
                    blocked[rid] = "refresh_unavailable"
        # Successful siblings cannot clear an active provider blocker. The
        # reloaded projection provides the same validated gates as the view.
        for page in range(1, (len(metadata) + 199) // 200 + 1):
            final_view = scoped.load(VerifiedModelQuery(page=page, page_size=200))
            for row in final_view.rows:
                if row.route_id in wanted and row.status.value in {"FAILED", "UNAVAILABLE"}:
                    blocked[row.route_id] = "refresh_" + row.status.value.lower()
        return blocked or None

    return hook
