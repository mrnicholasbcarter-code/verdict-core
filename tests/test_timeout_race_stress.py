"""Stress test: run the original flaky test 50x under CPU load."""

from __future__ import annotations

import threading


def _busy_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        _ = sum(range(10_000))


def test_timeout_race_50x_under_cpu_stress() -> None:
    """Run the original timeout assertion 50 times while 4 threads burn CPU."""
    from tests.test_tool_qualification import (
        test_tool_cancellation_consent_and_error_normalization_fail_closed,
    )

    stop = threading.Event()
    workers = [threading.Thread(target=_busy_loop, args=(stop,), daemon=True) for _ in range(4)]
    for w in workers:
        w.start()

    try:
        for _ in range(50):
            test_tool_cancellation_consent_and_error_normalization_fail_closed()
    finally:
        stop.set()
        for w in workers:
            w.join(timeout=1.0)
