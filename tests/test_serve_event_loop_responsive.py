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


def test_a_closed_lane_still_takes_cleanup_behind_its_queued_jobs_while_its_worker_runs() -> None:
    """``submit`` refuses new work once closed; a cleanup write for an existing row is not new."""
    ran: list[tuple[str, str]] = []

    def record(label: str) -> str:
        ran.append((label, threading.current_thread().name))
        return label

    async def scenario() -> None:
        lane = StateLane()
        gate = threading.Event()
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))
        queued = asyncio.ensure_future(lane.run(record, "queued"))
        await asyncio.sleep(0.05)
        closing = asyncio.ensure_future(lane.aclose(timeout=10))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError):
            lane.submit(record, "refused")
        cleanup = lane.submit_cleanup(record, "cleanup")
        gate.set()
        await closing  # the close waited for the cleanup it accepted while closing
        assert cleanup.result(timeout=0) == "cleanup"
        await busy
        await queued

    asyncio.run(scenario())
    assert [label for label, _ in ran] == ["queued", "cleanup"]
    assert all(name.startswith(LANE_PREFIX) for _, name in ran), ran


def test_cleanup_after_a_close_gave_up_on_a_stuck_job_waits_behind_it_on_the_lane_thread() -> None:
    """The bound ends the wait, not the queue: a cleanup never runs on the caller's thread.

    The caller is a request handler on the event loop; running its cleanup there would stall
    ``/health`` again and let the write overtake the jobs the stuck worker still holds.
    """
    ran: list[tuple[str, str]] = []

    def record(label: str) -> str:
        ran.append((label, threading.current_thread().name))
        return label

    def broken() -> None:
        raise ValueError("the write failed")

    async def scenario() -> None:
        lane = StateLane()
        stuck = threading.Event()
        job = asyncio.ensure_future(lane.run(stuck.wait, 10))
        queued = asyncio.ensure_future(lane.run(record, "queued"))
        await asyncio.sleep(0.05)
        await lane.aclose(timeout=0.05)  # gives up waiting for the stuck job
        done = lane.submit_cleanup(record, "cleanup")
        failed = lane.submit_cleanup(broken)
        await asyncio.sleep(0.05)
        assert not done.done() and not failed.done(), "a cleanup overtook the stuck job"
        assert ran == [], "a cleanup ran on the caller's thread while the lane was stuck"
        stuck.set()
        await job
        await queued
        assert await asyncio.wrap_future(done) == "cleanup"
        assert isinstance(failed.exception(timeout=5), ValueError), "the error was lost"

    asyncio.run(scenario())
    assert [label for label, _ in ran] == ["queued", "cleanup"], "a cleanup overtook queued work"
    assert all(name.startswith(LANE_PREFIX) for _, name in ran), ran


def test_a_cleanup_after_the_lane_drained_gets_a_fresh_worker_that_retires_again() -> None:
    ran: list[tuple[str, str]] = []

    def record(label: str) -> str:
        ran.append((label, threading.current_thread().name))
        return label

    async def scenario() -> StateLane:
        lane = StateLane()
        await lane.aclose()  # idle: the worker retires
        assert await lane.run_cleanup(record, "late") == "late"
        assert await lane.run_cleanup(record, "later") == "later"
        return lane

    lane = asyncio.run(scenario())
    assert ran == [("late", ran[0][1]), ("later", ran[1][1])]
    assert all(name.startswith(LANE_PREFIX) for _, name in ran), ran
    assert lane.closed
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        t.name.startswith(LANE_PREFIX) for t in threading.enumerate()
    ):
        time.sleep(0.02)
    assert not [t for t in threading.enumerate() if t.name.startswith(LANE_PREFIX)]


def test_a_cleanup_that_cannot_be_queued_fails_its_future_and_never_runs_on_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``submit_cleanup`` is called inside a ``CancelledError`` handler: it must not raise."""
    ran: list[str] = []
    lane = StateLane()

    def refuse(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cannot schedule new futures after interpreter shutdown")

    monkeypatch.setattr(ThreadPoolExecutor, "submit", refuse)
    future = lane.submit_cleanup(ran.append, "x")

    assert isinstance(future.exception(timeout=0), RuntimeError)
    assert ran == [], "the cleanup ran on the caller's thread"


def test_a_lane_is_drained_only_when_closed_and_every_job_has_finished() -> None:
    async def scenario() -> None:
        lane = StateLane()
        gate = threading.Event()
        assert not lane.drained
        busy = asyncio.ensure_future(lane.run(gate.wait, 10))
        await asyncio.sleep(0.05)
        await lane.aclose(timeout=0.05)
        assert lane.closed and not lane.drained
        gate.set()
        await busy
        assert lane.drained

    asyncio.run(scenario())


def test_cleanups_across_worker_retirements_run_once_serially_and_in_order() -> None:
    """A closed lane retires its worker whenever it is idle, and a cleanup restarts it.

    Submitting from the caller's thread while each job finishes and the worker retires drives the
    retire/restart race: no cleanup may be lost, run twice, overlap another, or run out of order.
    """
    count = 300
    ran: list[int] = []
    names: set[str] = set()
    active = 0
    overlaps = 0
    guard = threading.Lock()

    def job(index: int) -> int:
        nonlocal active, overlaps
        with guard:
            active += 1
            overlaps += active > 1
        names.add(threading.current_thread().name)
        ran.append(index)
        with guard:
            active -= 1
        return index

    async def scenario() -> None:
        lane = StateLane()
        await lane.aclose()
        futures = []
        for index in range(count):
            futures.append(lane.submit_cleanup(job, index))
            if index % 3 == 0:
                await asyncio.wrap_future(futures[-1])  # let it finish: the worker may retire
        for future in futures:
            assert await asyncio.wait_for(asyncio.wrap_future(future), 10) is not None

    asyncio.run(scenario())
    assert ran == list(range(count)) and overlaps == 0
    assert all(name.startswith(LANE_PREFIX) for name in names), names


def test_a_write_to_an_existing_row_after_the_app_shut_down_with_a_busy_worker_queues_behind_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The lifespan has ended and cleared its lane references, but the close gave up on a job.

    A handler still finishing then (a cancelled request, a stream that ends) writes through the
    same helpers.  They must queue behind the held job on its worker, in order, and not run
    inline on the event loop, where the write would overtake the job and stall the loop again.
    """
    _Harness(monkeypatch, _service(), tmp_path)
    service = _service()
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(api, "draining_state_lane_instance", None, raising=False)
    real_aclose = StateLane.aclose

    async def quick_close(self: StateLane, *, timeout: float = 10.0) -> None:
        await real_aclose(self, timeout=0.05)  # the lifespan's bound, shortened for the test

    monkeypatch.setattr(StateLane, "aclose", quick_close)
    order: list[tuple[str, str]] = []

    def record(label: str) -> str:
        order.append((label, threading.current_thread().name))
        return label

    monkeypatch.setattr(
        api, "_finish_evidence", lambda evidence, event, key=None: record(f"finish:{event}")
    )
    monkeypatch.setattr(
        api, "_cancelled_event", lambda evidence, *, event_prefix, features: f"{event_prefix}_gone"
    )
    stuck = threading.Event()

    async def scenario() -> None:
        context = contextlib.asynccontextmanager(api.lifespan)(api.app)
        await context.__aenter__()
        lane = api.state_lane_instance
        assert lane is not None
        busy = asyncio.ensure_future(lane.run(stuck.wait, 10))
        queued = asyncio.ensure_future(lane.run(record, "queued"))
        await asyncio.sleep(0.05)
        await context.__aexit__(None, None, None)  # the close gives up on the held job
        assert api.state_lane_instance is None and service.state_lane is None
        write = asyncio.ensure_future(api._write_row_on_lane(record, "write"))
        await asyncio.sleep(0)  # the write is submitted now, before the terminal event below
        api._queue_cancelled_terminal("evidence", "key", event_prefix="chat", features={})
        await asyncio.sleep(0.05)
        assert order == [], f"a write to an existing row ran ahead of the held job: {order}"
        assert not write.done()
        stuck.set()
        await busy
        await queued
        assert await asyncio.wait_for(write, 10) == "write"
        deadline = time.monotonic() + 5
        while len(order) < 3 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)

    asyncio.run(scenario())
    assert [label for label, _ in order] == ["queued", "write", "finish:chat_gone"], order
    assert all(name.startswith(LANE_PREFIX) for _, name in order), order


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hold_site: str, *, lane_close: str = "never"
) -> tuple[_Harness, list[tuple[str, str]]]:
    """Cancel a request task while ``hold_site`` is held on the lane; return what was recorded.

    ``lane_close`` is when the app's lane is closed relative to the cancellation: ``never``,
    ``after_cancel``, ``before_cancel`` (closed, its worker still running), or
    ``worker_stopped_before_cancel`` (closed and its bounded wait spent: the close gave up on
    the held job, which the worker still runs; a cleanup queues behind it).
    """
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
                lane = api.state_lane_instance
                assert lane is not None
                closing: asyncio.Future[None] | None = None
                if lane_close == "before_cancel":
                    closing = asyncio.ensure_future(lane.aclose(timeout=10))
                    await asyncio.sleep(0.05)
                    assert lane.closed
                elif lane_close == "worker_stopped_before_cancel":
                    await lane.aclose(timeout=0.05)  # gives up on the held job and stops the worker
                    assert lane.closed
                request.cancel()  # the request task goes away while its lane job is held
                with contextlib.suppress(asyncio.CancelledError):
                    await request
                if lane_close == "after_cancel":
                    # The app starts shutting down while the held job is still running: the lane
                    # takes no new job, yet the cancelled event queued before must still land.
                    closing = asyncio.ensure_future(lane.aclose(timeout=10))
                    await asyncio.sleep(0.05)
                release.set()
                if closing is not None:
                    await closing  # a clean close has waited for every job, cleanups included
                elif lane_close == "never":
                    await lane.run(lambda: None)  # everything queued before this has now run
                deadline = time.monotonic() + 5
                while ("chat_completion_cancelled", "cancelled") not in finished:
                    if time.monotonic() > deadline:
                        break  # the caller's assertion reports what was recorded
                    await asyncio.sleep(0.02)

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
    harness, finished = _cancel_while_held(
        monkeypatch, tmp_path, "start", lane_close="after_cancel"
    )

    assert ("chat_completion_cancelled", "cancelled") in finished, finished
    assert harness.transport.requests == []


@pytest.mark.parametrize("hold_site", ["start", "permit"])
@pytest.mark.parametrize("lane_close", ["before_cancel", "worker_stopped_before_cancel"])
def test_a_row_is_ended_when_the_lane_closes_before_its_waiter_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, hold_site: str, lane_close: str
) -> None:
    """Shutdown first, cancellation second, the job still held: the accepted job's row is ended.

    The permit's cleanup is accepted behind the held job, also after the close gave up waiting
    for it; the start job ends its own row.
    """
    harness, finished = _cancel_while_held(monkeypatch, tmp_path, hold_site, lane_close=lane_close)

    assert finished.count(("chat_completion_cancelled", "cancelled")) == 1, finished
    assert harness.transport.requests == [], "a cancelled request reached the upstream"


def _close_the_lane_during_upstream_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, cancel: bool
) -> tuple[_Harness, list[tuple[str, str]], Any]:
    """Close the lane while a request awaits its upstream call, then cancel it or let it finish.

    Returns the harness, the evidence events recorded, and the response (``None`` when the
    request was cancelled or raised).  The lane is idle when it closes, so its worker has
    retired by the time the request writes how it ended; the write gets a fresh one.
    """
    import httpx

    from tests.relay_proof_helpers import assignment_headers

    harness = _Harness(monkeypatch, _service(), tmp_path)
    finished: list[tuple[str, str]] = []
    real_finish = api._finish_evidence

    def recording_finish(evidence: Any, event: Any, key: Any = None) -> Any:
        finished.append((event.event_type, event.outcome))
        return real_finish(evidence, event, key)

    monkeypatch.setattr(api, "_finish_evidence", recording_finish)
    real_upstream = harness.transport.handle_async_request
    in_flight = asyncio.Event
    state: dict[str, Any] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        state["entered"].set()
        await asyncio.wait_for(state["release"].wait(), HOLD_SECONDS * 4)
        return await real_upstream(request)

    monkeypatch.setattr(harness.transport, "handle_async_request", upstream)

    async def scenario() -> Any:
        state["entered"], state["release"] = in_flight(), in_flight()
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
                await asyncio.wait_for(state["entered"].wait(), 10)
                lane = api.state_lane_instance
                assert lane is not None
                await lane.aclose(timeout=1)
                assert lane.closed
                if cancel:
                    request.cancel()
                else:
                    state["release"].set()
                response = None
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    response = await request
                deadline = time.monotonic() + 5
                while (
                    not any(outcome != "unknown" for _, outcome in finished)
                    and time.monotonic() < deadline
                ):
                    await asyncio.sleep(0.02)
                return response

    response = asyncio.run(scenario())
    return harness, finished, response


def test_a_request_cancelled_in_its_upstream_call_after_the_lane_closed_still_ends_its_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, finished, _ = _close_the_lane_during_upstream_io(monkeypatch, tmp_path, cancel=True)

    assert finished == [("chat_completion_cancelled", "cancelled")], finished


def test_a_request_that_finishes_after_the_lane_closed_still_records_how_it_ended(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness, finished, response = _close_the_lane_during_upstream_io(
        monkeypatch, tmp_path, cancel=False
    )

    assert response is not None and response.status_code == 200, response
    assert len(harness.transport.requests) == 1
    assert ("chat_completion_buffered", "success") in finished, finished
    assert "cancelled" not in {outcome for _, outcome in finished}, finished


def _fake_evidence_calls(
    monkeypatch: pytest.MonkeyPatch, *, hold: threading.Event | None = None
) -> list[tuple[str, str | None, str]]:
    """Stand-ins for the evidence start and finish; each finish records its event, key, thread."""
    finished: list[tuple[str, str | None, str]] = []

    def start(decision: Any, **kwargs: Any) -> tuple[Any, str | None]:
        if hold is not None:
            hold.wait(HOLD_SECONDS * 4)
        return "evidence", "key"

    def finish(evidence: Any, event: Any, key: str | None = None) -> Any:
        finished.append((event, key, threading.current_thread().name))
        return evidence

    monkeypatch.setattr(api, "_start_evidence", start)
    monkeypatch.setattr(api, "_finish_evidence", finish)
    monkeypatch.setattr(
        api, "_cancelled_event", lambda evidence, *, event_prefix, features: f"{event_prefix}_gone"
    )
    return finished


def test_a_start_job_abandoned_before_it_finishes_ends_its_own_row_on_its_own_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane is closed and its close gave up: no row exists yet, so the job ends its own."""
    hold = threading.Event()
    finished = _fake_evidence_calls(monkeypatch, hold=hold)

    async def scenario() -> None:
        lane = StateLane()
        job = api._StartJob(event_prefix="chat", features={})
        future = lane.submit(job, object())
        await asyncio.sleep(0.05)
        await lane.aclose(timeout=0.05)
        monkeypatch.setattr(api, "state_lane_instance", lane)
        job.abandon()
        assert finished == [], "the row was ended before it was written"
        hold.set()
        assert await asyncio.wrap_future(future) == ("evidence", "key")

    asyncio.run(scenario())
    assert len(finished) == 1, finished
    event, key, thread_name = finished[0]
    assert (event, key) == ("chat_gone", "key") and thread_name.startswith(LANE_PREFIX)


def test_a_start_job_that_was_not_abandoned_leaves_its_row_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = _fake_evidence_calls(monkeypatch)

    async def scenario() -> None:
        lane = StateLane()
        job = api._StartJob(event_prefix="chat", features={})
        assert await asyncio.wrap_future(lane.submit(job, object())) == ("evidence", "key")
        await lane.aclose()

    asyncio.run(scenario())
    assert finished == []


@pytest.mark.parametrize("lane_state", ["open", "closed", "gone"])
def test_a_start_job_abandoned_after_it_finished_ends_its_row_once_whatever_the_lane_is(
    monkeypatch: pytest.MonkeyPatch, lane_state: str
) -> None:
    finished = _fake_evidence_calls(monkeypatch)
    main_thread = threading.current_thread().name

    async def scenario() -> None:
        lane = StateLane()
        job = api._StartJob(event_prefix="chat", features={})
        await asyncio.wrap_future(lane.submit(job, object()))
        if lane_state != "open":
            await lane.aclose(timeout=0.05)
        monkeypatch.setattr(api, "state_lane_instance", None if lane_state == "gone" else lane)
        job.abandon()
        if lane_state != "gone":
            await lane.run_cleanup(lambda: None)  # the cleanup was queued before this one
            await lane.aclose()

    asyncio.run(scenario())
    assert [(event, key) for event, key, _ in finished] == [("chat_gone", "key")], finished
    # With no lane at all nothing is running: the write is made where it is called.  A lane that
    # is open or closed (its worker retired) takes it on its worker.
    expected_thread = main_thread if lane_state == "gone" else LANE_PREFIX
    assert finished[0][2].startswith(expected_thread), finished


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
