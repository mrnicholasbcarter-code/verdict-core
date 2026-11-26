"""A handler a test installs for SIGTERM, SIGINT or SIGHUP does not outlive the test (#1363).

These handlers belong to the process, and an xdist worker runs many files in one process.
``scripts/prime_supervisor.py``'s ``main()`` installs a SIGTERM and a SIGHUP handler and never
gives them back, and several test files call it in process.  A later file in the same worker then
started with somebody else's handler, and a test of what a run does to SIGTERM failed in CI
only (``tests/test_orch_worker_reaping.py``).  ``tests/conftest.py`` restores them after every
test; this file pins that.

The two tests depend on running in file order, which pytest and ``--dist loadfile`` both keep;
a module-scoped fixture bounds the damage to this file if the restoring ever stops working.
Run alone, the second test passes without exercising anything.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator

import pytest

_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGINT", "SIGHUP") if hasattr(signal, name)
)

pytestmark = pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(),
    reason="signal handlers belong to the main thread",
)


def _callers_handler(signum: int, frame: object) -> None:  # pragma: no cover - never signalled
    raise AssertionError("the test never sends a signal")


@pytest.fixture(scope="module")
def _started_with() -> Iterator[dict[int, object]]:
    """What the handlers were when this file began; put back when it ends, whatever happened."""
    saved = {number: signal.getsignal(number) for number in _SIGNALS}
    try:
        yield saved
    finally:
        for number, handler in saved.items():
            if handler is not None:
                signal.signal(number, handler)


def test_a_test_that_leaves_a_handler_behind(_started_with: dict[int, object]) -> None:
    for number in _SIGNALS:
        signal.signal(number, _callers_handler)
        assert signal.getsignal(number) is _callers_handler


def test_the_next_test_starts_with_the_handlers_the_previous_one_started_with(
    _started_with: dict[int, object],
) -> None:
    now = {number: signal.getsignal(number) for number in _SIGNALS}
    assert now == _started_with, "a handler a test installed outlived it"
