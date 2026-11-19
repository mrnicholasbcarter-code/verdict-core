## Why

Issue #1149: the `test (3.10)` CI job sometimes hangs. The lifespan shutdown cancels the public-models refresh task and awaits it. Before Python 3.12, `asyncio.wait_for` returns the inner result and drops a cancel that arrives as the inner load finishes (bpo-42130). The refresh loop then keeps running, so `await task` never returns and shutdown hangs. The same race can hang `verdict serve` shutdown on Python 3.10 and 3.11.

## What Changes

- The lifespan sets a stop flag before it cancels the refresh task. The refresh loop checks the flag before each sleep and each load, so a lost cancel ends the loop at its next check.
- A regression test reproduces the lost cancel on every Python version and requires shutdown to finish within 5 seconds.

## Impact

- Code: `verdict/api.py` (lifespan refresh loop).
- Tests: `tests/test_relay_admission.py`.
- No change to admission, routing, the public catalog or its deadlines.
