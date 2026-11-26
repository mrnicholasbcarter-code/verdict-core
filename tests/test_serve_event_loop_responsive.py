"""SB1b (#1360): ``verdict serve`` keeps its event loop free while a request is decided.

The request path's blocking state work (the route decision, the evidence writes, the dispatch
permit and the dispatch observation) runs on ONE serial worker lane that the app creates and
shuts down, never on the event loop.  ``/health`` and the cheap refusals answer while a decision
is in progress; decisions and evidence writes keep one serial order; and nothing about what is
admitted, refused, recorded or sent upstream changes.

A hold in these tests is always a bounded wait (``HOLD_SECONDS``), so a regression is a failed
assertion about time, never a hang.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import verdict.admit_prove_confirm as admit_prove_confirm
import verdict.api as api
from tests.helpers import public_route_harness as route_harness
from tests.test_serve_worker_route_proof import FREE, SUBSCRIPTION, _Harness, _service
from verdict import intelligence
from verdict.evidence import DurableEvidenceStore
from verdict.execution_path import ExecutionPathError
from verdict.intelligence import IntelligenceService, StateLane
from verdict.orchestration.health_cache import HealthCache
from verdict.worker_assignment import VerifiedWorkerAssignment

LANE_PREFIX = "verdict-state-lane"
HOLD_SECONDS = 1.5
HEALTH_BUDGET_SECONDS = 0.2


def _on_lane() -> bool:
    return threading.current_thread().name.startswith(LANE_PREFIX)


class _Hold:
    """Holds the route decision inside a patched slow function until released (bounded)."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.entries: list[str | None] = []

    def install(self, monkeypatch: pytest.MonkeyPatch, *, first_only: bool = False) -> None:
        real = IntelligenceService._route_assigned
        hold = self

        def held(service: IntelligenceService, *args: Any, **kwargs: Any) -> Any:
            hold.entries.append(kwargs.get("request_id"))
            first = len(hold.entries) == 1
            hold.entered.set()
            if first or not first_only:
                hold.release.wait(HOLD_SECONDS)
            return real(service, *args, **kwargs)

        monkeypatch.setattr(IntelligenceService, "_route_assigned", held)


def test_health_and_cheap_refusals_answer_while_a_route_decision_is_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The acceptance check: /health under 0.2 s while a decision is held in a slow function."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    hold = _Hold()
    hold.install(monkeypatch)
    with TestClient(api.app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        post = pool.submit(harness.post, client, worker=True, model=FREE)
        assert hold.entered.wait(10), "the request never reached the route decision"
        started = time.perf_counter()  # timed only once the decision is being held
        health = client.get("/health")
        health_seconds = time.perf_counter() - started
        started = time.perf_counter()
        refused = client.get("/v1/models")  # no bearer: the cheap 401
        refused_seconds = time.perf_counter() - started
        hold.release.set()
        response = post.result(timeout=30)

    assert health.status_code == 200 and health.json()["status"] == "healthy"
    assert health_seconds < HEALTH_BUDGET_SECONDS, f"/health took {health_seconds:.2f}s"
    assert refused.status_code == 401
    assert refused_seconds < HEALTH_BUDGET_SECONDS, f"the 401 took {refused_seconds:.2f}s"
    # The held request still completes, and is dispatched upstream exactly once.
    assert response.status_code == 200, response.text
    assert [r["body"]["model"] for r in harness.transport.requests] == [FREE]


def test_the_request_path_state_work_runs_on_the_one_lane_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each named piece of state work ran on the lane thread, never the event loop's."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    ran: dict[str, set[str]] = {}

    def spied(label: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ran.setdefault(label, set()).add(threading.current_thread().name)
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(
        IntelligenceService, "_route_assigned", spied("route", IntelligenceService._route_assigned)
    )
    monkeypatch.setattr(api, "_start_evidence", spied("start", api._start_evidence))
    monkeypatch.setattr(api, "_finish_evidence", spied("finish", api._finish_evidence))
    monkeypatch.setattr(
        HealthCache, "commit_dispatch", spied("permit", HealthCache.commit_dispatch)
    )
    monkeypatch.setattr(
        admit_prove_confirm,
        "record_dispatch_observation",
        spied("observation", admit_prove_confirm.record_dispatch_observation),
    )
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=FREE)

    assert response.status_code == 200, response.text
    assert set(ran) == {"route", "start", "finish", "permit", "observation"}
    threads = {name for names in ran.values() for name in names}
    assert len(threads) == 1, f"state work ran on more than one thread: {ran}"
    (thread,) = threads
    assert thread.startswith(LANE_PREFIX), f"ran on {thread!r}, not the lane"


def test_a_denial_runs_on_the_lane_and_makes_no_upstream_call_and_no_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _Harness(monkeypatch, _service(), tmp_path)
    ran: list[str] = []
    real = IntelligenceService._route_assigned

    def spied(service: IntelligenceService, *args: Any, **kwargs: Any) -> Any:
        ran.append(threading.current_thread().name)
        return real(service, *args, **kwargs)

    monkeypatch.setattr(IntelligenceService, "_route_assigned", spied)
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model="openrouter/not/in-the-catalog:free")

    assert response.status_code == 503, response.text
    assert harness.transport.requests == [], "upstream was contacted for a denied request"
    assert harness.minted == [], "a proof was minted for a denied request"
    assert harness.decisions[-1].decision == "denied"
    assert len(ran) == 1 and ran[0].startswith(LANE_PREFIX)


def test_request_time_and_assignment_are_visible_inside_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fixed request instant and the verified assignment reach the lane's decision."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    bound: list[Any] = []
    real_clock = IntelligenceService._request_clock

    @contextlib.contextmanager
    def clock(service: IntelligenceService) -> Iterator[None]:
        with real_clock(service):
            bound.append(intelligence._REQUEST_NOW.get())  # the instant, bound loop-side
            yield

    monkeypatch.setattr(IntelligenceService, "_request_clock", clock)
    seen: dict[str, Any] = {}
    real = IntelligenceService._route_assigned

    def spied(
        service: IntelligenceService,
        task: str,
        criticality: Any,
        context: dict[str, Any],
        assignment: VerifiedWorkerAssignment,
        **kwargs: Any,
    ) -> Any:
        seen["thread"] = threading.current_thread().name
        seen["now"] = intelligence._REQUEST_NOW.get()
        seen["assignment"] = assignment
        return real(service, task, criticality, context, assignment, **kwargs)

    monkeypatch.setattr(IntelligenceService, "_route_assigned", spied)
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=SUBSCRIPTION)

    assert response.status_code == 200, response.text
    assert seen["thread"].startswith(LANE_PREFIX)
    assert len(bound) == 1 and bound[0] is not None
    assert seen["now"] is bound[0], "the lane did not see the instant bound for this request"
    assert seen["assignment"].route_id == SUBSCRIPTION
    assert response.headers["x-verdict-model"] == SUBSCRIPTION  # the assignment pinned the route


def test_evidence_writes_keep_one_serial_order_for_two_concurrent_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two requests in flight: one lane job at a time, in arrival order, each request in order."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    hold = _Hold()
    hold.install(monkeypatch, first_only=True)
    log: list[tuple[str, str]] = []
    key_to_request: dict[str, str] = {}
    active = 0
    peak = 0
    guard = threading.Lock()

    @contextlib.contextmanager
    def one_at_a_time() -> Iterator[None]:
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            yield
        finally:
            with guard:
                active -= 1

    real_put = DurableEvidenceStore.put
    real_append = DurableEvidenceStore.append_event

    def put(store: DurableEvidenceStore, evidence: Any, *, scope: str) -> str:
        with one_at_a_time():
            key = real_put(store, evidence, scope=scope)
            request_id = str(evidence.routing_decision.request_id)
            key_to_request[key] = request_id
            log.append((request_id, "put"))
            return key

    def append_event(store: DurableEvidenceStore, key: str, event: Any, *, scope: str) -> Any:
        with one_at_a_time():
            log.append((key_to_request[key], event.event_type or ""))
            return real_append(store, key, event, scope=scope)

    monkeypatch.setattr(DurableEvidenceStore, "put", put)
    monkeypatch.setattr(DurableEvidenceStore, "append_event", append_event)

    with TestClient(api.app) as client, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            harness.post,
            client,
            worker=True,
            model=FREE,
            headers={**_worker_headers(FREE), "x-verdict-request-id": "order-one"},
        )
        assert hold.entered.wait(10)
        second = pool.submit(
            harness.post,
            client,
            worker=True,
            model=FREE,
            headers={**_worker_headers(FREE), "x-verdict-request-id": "order-two"},
        )
        time.sleep(0.3)  # the second request is queued behind the held decision on the lane
        assert hold.entries == [hold.entries[0]], "the second decision ran beside the held one"
        hold.release.set()
        responses = [first.result(timeout=30), second.result(timeout=30)]

    assert [r.status_code for r in responses] == [200, 200]
    assert len(hold.entries) == 2 and hold.entries[0] != hold.entries[1]
    first_id, second_id = hold.entries  # decisions ran in arrival order
    puts = [request for request, kind in log if kind == "put"]
    assert puts == [first_id, second_id], "evidence rows were written out of arrival order"
    for request_id in (first_id, second_id):
        kinds = [kind for request, kind in log if request == request_id]
        assert kinds[0] == "put" and kinds[-1].endswith("_buffered"), kinds
        assert any(kind.endswith("_attempt_completed") for kind in kinds), kinds
    assert peak == 1, "two state-work jobs ran at the same time"


def _worker_headers(route: str) -> dict[str, str]:
    from tests.relay_proof_helpers import assignment_headers

    return assignment_headers(route)


def test_the_route_endpoint_state_work_also_runs_on_the_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``POST /v1/route`` decides and records on the same lane, not beside it."""
    service = route_harness.install(monkeypatch, tmp_path)
    ran: dict[str, str] = {}

    def spied(label: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            ran[label] = threading.current_thread().name
            return real(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(service, "_route", spied("route", service._route))
    monkeypatch.setattr(api, "_start_evidence", spied("start", api._start_evidence))
    monkeypatch.setattr(api, "_finish_evidence", spied("finish", api._finish_evidence))
    candidates = [route_harness.candidate(route, is_free=True) for route in route_harness.ROUTES]
    with route_harness.client() as client:
        response = client.post("/v1/route", json=route_harness.route_body(candidates))
        alias = client.post("/route", json=route_harness.route_body(candidates))

    assert response.status_code == 200, response.text
    assert alias.status_code == 200, alias.text
    assert set(ran) == {"route", "start", "finish"}
    assert all(name.startswith(LANE_PREFIX) for name in ran.values()), ran


@pytest.mark.parametrize(
    ("error", "status"),
    [(ExecutionPathError("no strategy for this task"), 400), (RuntimeError("boom"), 500)],
    ids=["execution-path-error-is-400", "unexpected-error-is-500"],
)
def test_an_error_inside_the_lane_surfaces_as_the_same_http_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception, status: int
) -> None:
    harness = _Harness(monkeypatch, _service(), tmp_path)

    def broken(service: IntelligenceService, *args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(IntelligenceService, "_route_assigned", broken)
    with TestClient(api.app, raise_server_exceptions=False) as client:
        response = harness.post(client, worker=True, model=FREE)

    assert response.status_code == status, response.text
    if status == 400:
        assert "no strategy for this task" in response.text
    assert harness.transport.requests == []
    assert harness.minted == []


def test_the_lane_belongs_to_the_app_and_is_shut_down_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Harness(monkeypatch, _service(), tmp_path)
    with TestClient(api.app) as client:
        lane = api.state_lane_instance
        assert lane is not None and not lane.closed
        assert api.intelligence_instance is not None
        assert api.intelligence_instance.state_lane is lane
        assert client.get("/health").status_code == 200
        assert lane.submit(threading.current_thread).result(5).name.startswith(LANE_PREFIX)
    assert lane.closed
    assert api.state_lane_instance is None
    with pytest.raises(RuntimeError):
        lane.submit(lambda: None)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        t.name.startswith(LANE_PREFIX) for t in threading.enumerate()
    ):
        time.sleep(0.02)
    assert not [t for t in threading.enumerate() if t.name.startswith(LANE_PREFIX)]


def test_a_service_without_a_lane_decides_inline_as_before() -> None:
    """Callers outside ``verdict serve`` (CLI, orchestration) attach no lane: nothing changes."""
    service = _service()
    assert service.state_lane is None
    decision = asyncio.run(service.route("summarize this paragraph", criticality="low"))
    assert decision.decision in {"selected", "denied"}


# -- the lane itself ---------------------------------------------------------------------------


def test_lane_runs_jobs_one_at_a_time_in_submission_order_with_the_callers_context() -> None:
    from contextvars import ContextVar

    from verdict.intelligence import StateLane

    marker: ContextVar[str] = ContextVar("marker", default="unset")
    order: list[tuple[int, str, str]] = []

    async def scenario() -> None:
        lane = StateLane()
        marker.set("request-context")

        def job(number: int) -> int:
            order.append((number, marker.get(), threading.current_thread().name))
            time.sleep(0.01)
            return number

        results = await asyncio.gather(*(lane.run(job, n) for n in range(5)))
        assert results == [0, 1, 2, 3, 4]
        await lane.aclose()

    asyncio.run(scenario())
    assert [n for n, _, _ in order] == [0, 1, 2, 3, 4]
    assert {value for _, value, _ in order} == {"request-context"}
    assert len({name for _, _, name in order}) == 1
    assert order[0][2].startswith(LANE_PREFIX)


def test_a_cancelled_waiter_does_not_drop_the_job_it_queued() -> None:
    """The terminal evidence write of a disconnected request still lands, in order."""
    from verdict.intelligence import StateLane

    ran: list[str] = []

    async def scenario() -> None:
        lane = StateLane()
        gate = threading.Event()
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))  # occupies the worker
        await asyncio.sleep(0.05)
        waiter = asyncio.ensure_future(lane.run(ran.append, "terminal"))
        later = asyncio.ensure_future(lane.run(ran.append, "after"))
        await asyncio.sleep(0.05)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        gate.set()
        await busy
        await later
        await lane.aclose()

    asyncio.run(scenario())
    assert ran == ["terminal", "after"]


def test_closing_the_lane_drains_queued_jobs_within_a_bound_and_then_refuses_new_ones() -> None:
    from verdict.intelligence import StateLane

    ran: list[str] = []

    async def scenario() -> None:
        lane = StateLane()
        gate = threading.Event()
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))
        await asyncio.sleep(0.05)
        queued = asyncio.ensure_future(lane.run(ran.append, "queued"))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        closing = asyncio.ensure_future(lane.aclose(timeout=5))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError):
            await lane.run(ran.append, "too-late")
        gate.set()
        await closing
        await busy
        await queued
        assert time.monotonic() - started < 4
        assert lane.closed

    asyncio.run(scenario())
    assert ran == ["queued"]


def test_closing_a_lane_with_a_stuck_job_gives_up_waiting_but_drops_nothing() -> None:
    """The bound ends the wait, not the work: a queued terminal write still runs afterwards."""
    from verdict.intelligence import StateLane

    ran: list[str] = []

    async def scenario() -> float:
        lane = StateLane()
        stuck = threading.Event()
        job = asyncio.ensure_future(lane.run(stuck.wait, 3))
        await asyncio.sleep(0.05)
        queued = asyncio.ensure_future(lane.run(ran.append, "queued"))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await lane.aclose(timeout=0.2)
        elapsed = time.monotonic() - started
        assert ran == [], "the queued job ran while the lane was stuck"
        stuck.set()
        await job
        await queued
        return elapsed

    assert asyncio.run(scenario()) < 1.5
    assert ran == ["queued"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        t.name.startswith(LANE_PREFIX) for t in threading.enumerate()
    ):
        time.sleep(0.02)
    assert not [t for t in threading.enumerate() if t.name.startswith(LANE_PREFIX)]


def test_a_cancelled_close_still_stops_the_worker() -> None:
    from verdict.intelligence import StateLane

    ran: list[str] = []

    async def scenario() -> StateLane:
        lane = StateLane()
        gate = threading.Event()
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))
        queued = asyncio.ensure_future(lane.run(ran.append, "queued"))
        await asyncio.sleep(0.05)
        closing = asyncio.ensure_future(lane.aclose(timeout=30))
        await asyncio.sleep(0.05)
        closing.cancel()  # app teardown itself is cancelled while jobs are pending
        with contextlib.suppress(asyncio.CancelledError):
            await closing
        assert lane.closed
        with pytest.raises(RuntimeError):
            lane.submit(lambda: None)
        gate.set()
        await busy
        await queued
        return lane

    lane = asyncio.run(scenario())  # kept alive: an unreferenced executor stops its own worker
    assert ran == ["queued"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        t.name.startswith(LANE_PREFIX) for t in threading.enumerate()
    ):
        time.sleep(0.02)
    assert not [t for t in threading.enumerate() if t.name.startswith(LANE_PREFIX)], (
        "the worker was left running after its close was cancelled"
    )
    assert lane.closed


def test_the_lifespan_releases_the_lane_when_the_app_exits_with_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Harness(monkeypatch, _service(), tmp_path)
    service = _service()
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)

    async def scenario() -> StateLane:
        with pytest.raises(RuntimeError, match="app failed"):
            async with contextlib.asynccontextmanager(api.lifespan)(api.app):
                lane = api.state_lane_instance
                assert lane is not None and service.state_lane is lane
                raise RuntimeError("app failed")
        return lane

    lane = asyncio.run(scenario())
    assert lane.closed
    assert api.state_lane_instance is None and service.state_lane is None


def test_a_stream_terminal_is_awaited_on_the_lane_even_when_the_closer_is_cancelled() -> None:
    """An async terminal callback is awaited; a cancelled ``aclose`` still lands its event once."""
    from verdict.intelligence import StateLane

    landed: list[tuple[str, str]] = []

    async def scenario() -> None:
        lane = StateLane()
        gate = threading.Event()

        async def on_terminal(event: Any) -> None:
            await lane.run(lambda: landed.append((event, threading.current_thread().name)))

        def factory(event_type: str, outcome: str, phase: str, cleanup: Any, error: Any) -> Any:
            return event_type

        class Upstream:
            async def __anext__(self) -> bytes:
                raise StopAsyncIteration

            async def aclose(self) -> None:
                return None

        adapter = api._EvidenceStreamAdapter(
            Upstream(),  # type: ignore[arg-type]
            on_terminal=on_terminal,
            event_factory=factory,
            outcome_of=lambda: None,
        )
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))
        await asyncio.sleep(0.05)
        closer = asyncio.ensure_future(adapter.aclose())
        await asyncio.sleep(0.05)
        closer.cancel()  # the client went away while the lane was busy
        with contextlib.suppress(asyncio.CancelledError):
            await closer
        gate.set()
        await busy
        await adapter.aclose()  # a second close adds no second terminal event
        await lane.aclose()

    asyncio.run(scenario())
    assert [event for event, _ in landed] == ["chat_completion_stream_aborted"]
    assert landed[0][1].startswith(LANE_PREFIX)


# -- cancellation of a request task while it waits on the lane --------------------------------


def _cancel_while_held(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hold_site: str, *, close_lane: bool = False
) -> tuple[_Harness, list[tuple[str, str]]]:
    """Cancel a request task while ``hold_site`` is held on the lane; return what was recorded."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished: list[tuple[str, str]] = []
    real_finish = api._finish_evidence

    def recording_finish(evidence: Any, event: Any, key: Any = None) -> Any:
        finished.append((event.event_type, event.outcome))
        return real_finish(evidence, event, key)

    monkeypatch.setattr(api, "_finish_evidence", recording_finish)

    def hold(real: Callable[..., Any]) -> Callable[..., Any]:
        def held(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            release.wait(HOLD_SECONDS * 4)
            return real(*args, **kwargs)

        return held

    if hold_site == "start":
        monkeypatch.setattr(api, "_start_evidence", hold(api._start_evidence))
    else:
        monkeypatch.setattr(HealthCache, "commit_dispatch", hold(HealthCache.commit_dispatch))

    import httpx

    from tests.relay_proof_helpers import assignment_headers

    async def scenario() -> None:
        async with api.app.router.lifespan_context(api.app):
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                request = asyncio.ensure_future(
                    client.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": "summarize this paragraph"}],
                            "max_tokens": 16,
                            "criticality": "low",
                            "model": FREE,
                        },
                        headers=assignment_headers(FREE),
                    )
                )
                assert await asyncio.to_thread(entered.wait, 10), "the hold was never reached"
                request.cancel()  # the request task goes away while its lane job is held
                with contextlib.suppress(asyncio.CancelledError):
                    await request
                lane = api.state_lane_instance
                assert lane is not None
                if close_lane:
                    # The app starts shutting down while the held job is still running: the lane
                    # takes no new job, yet the cancelled event queued before must still land.
                    closing = asyncio.ensure_future(lane.aclose(timeout=10))
                    await asyncio.sleep(0.05)
                    release.set()
                    await closing
                else:
                    release.set()
                    await lane.run(lambda: None)  # everything queued before this has now run

    asyncio.run(scenario())
    return harness, finished


@pytest.mark.parametrize("hold_site", ["start", "permit"])
def test_a_task_cancelled_while_its_row_is_being_started_or_its_permit_committed_ends_the_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hold_site: str
) -> None:
    harness, finished = _cancel_while_held(monkeypatch, tmp_path, hold_site)

    assert ("chat_completion_cancelled", "cancelled") in finished, finished
    assert harness.transport.requests == [], "a cancelled request reached the upstream"


def test_a_started_row_is_ended_even_when_the_lane_closes_before_its_start_job_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Shutdown begins while the start job is held and its waiter is already cancelled."""
    harness, finished = _cancel_while_held(monkeypatch, tmp_path, "start", close_lane=True)

    assert ("chat_completion_cancelled", "cancelled") in finished, finished
    assert harness.transport.requests == []


def test_a_cancelled_lifespan_teardown_still_clears_the_lane_references(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _Harness(monkeypatch, _service(), tmp_path)
    service = _service()
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    gate = threading.Event()

    async def scenario() -> StateLane:
        context = contextlib.asynccontextmanager(api.lifespan)(api.app)
        await context.__aenter__()
        lane = api.state_lane_instance
        assert lane is not None
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))
        await asyncio.sleep(0.05)
        teardown = asyncio.ensure_future(context.__aexit__(None, None, None))
        await asyncio.sleep(0.05)
        teardown.cancel()  # the app's own teardown is cancelled while the lane drains
        with contextlib.suppress(asyncio.CancelledError):
            await teardown
        gate.set()
        await busy
        return lane

    lane = asyncio.run(scenario())
    assert lane.closed
    assert api.state_lane_instance is None and service.state_lane is None


def test_buffered_bookkeeping_keeps_its_order_in_one_lane_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Buffered: the observation, then the terminal event, both on the lane."""
    harness = _Harness(monkeypatch, _service(), tmp_path)
    order: list[tuple[str, str, str]] = []
    real_finish = api._finish_evidence
    real_observe = admit_prove_confirm.record_dispatch_observation

    def finish(evidence: Any, event: Any, key: Any = None) -> Any:
        if str(event.event_type).endswith(("_buffered", "_streamed")):
            order.append(("finish", event.event_type, threading.current_thread().name))
        return real_finish(evidence, event, key)

    def observe(*args: Any, **kwargs: Any) -> Any:
        order.append(("observe", "", threading.current_thread().name))
        return real_observe(*args, **kwargs)

    monkeypatch.setattr(api, "_finish_evidence", finish)
    monkeypatch.setattr(admit_prove_confirm, "record_dispatch_observation", observe)
    with TestClient(api.app) as client:
        response = harness.post(client, worker=True, model=FREE)
    assert response.status_code == 200, response.text
    assert [kind for kind, _, _ in order] == ["observe", "finish"]
    assert {thread for _, _, thread in order} == {order[0][2]}
    assert order[0][2].startswith(LANE_PREFIX)


def test_stream_bookkeeping_keeps_its_order_in_one_lane_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream: the terminal event, then the observation, both on the lane."""
    from tests.test_proxy import _configure_test_app
    from tests.test_stream_relay_wiring import CHAT_STANDARD, StreamTransport

    _configure_test_app(monkeypatch, StreamTransport(CHAT_STANDARD))
    order: list[tuple[str, str]] = []
    real_finish = api._finish_evidence
    real_observe = admit_prove_confirm.record_dispatch_observation

    def finish(evidence: Any, event: Any, key: Any = None) -> Any:
        if str(event.event_type).endswith("_streamed"):
            order.append(("finish", threading.current_thread().name))
        return real_finish(evidence, event, key)

    def observe(*args: Any, **kwargs: Any) -> Any:
        order.append(("observe", threading.current_thread().name))
        return real_observe(*args, **kwargs)

    monkeypatch.setattr(api, "_finish_evidence", finish)
    monkeypatch.setattr(admit_prove_confirm, "record_dispatch_observation", observe)
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "preserve all fields"}], "stream": True},
        )
    assert response.status_code == 200
    assert [kind for kind, _ in order] == ["finish", "observe"]
    assert {thread for _, thread in order} == {order[0][1]}
    assert order[0][1].startswith(LANE_PREFIX)
