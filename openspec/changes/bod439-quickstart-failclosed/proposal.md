# Proposal: BOD-439 quickstart route probe must fail closed

## Why

`scripts/quickstart.py` printed `RESULT: PASS` after a route probe that did not succeed. Parent proof on base `5358381d` got HTTP 503 with exit 0 and `RESULT: PASS` (G7 quickstart log). A failed probe must not be reported as a successful quickstart.

## What Changes

- The existing route step accepts only a child exit of 0, a first stdout line of `200`, and a JSON object whose `decision` is a non-empty string other than `denied`.
- Any other status, a non-zero child, malformed JSON, a missing or empty decision, or `decision` of `denied` exits non-zero and prints `RESULT: FAIL`.
- HTTP 200 with a non-denied decision may continue. API auth, request payload, and lifespan are unchanged.

## Capabilities

### New Capabilities

- `quickstart-route-probe`: The clean-environment quickstart reports a route probe as success only when the probe response is a successful non-denied routing decision.

### Modified Capabilities

## Impact

- `scripts/quickstart.py` route-step success check only.
- New behavioral tests in `tests/test_quickstart_route_probe.py`.
- No API, gate, dependency, or install change. This does not close G7.1 or the MVP.

## Evidence boundary

KNOWN: parent proof on base `5358381d` recorded HTTP 503, exit 0, and `RESULT: PASS` in the G7 quickstart log. KNOWN: the route step already runs a child `TestClient` POST and previously continued when that child exited 0. INFERRED: requiring child exit 0, a first line of `200`, and a non-empty non-`denied` `decision` stops that false pass for the cases covered by the local tests. NOT AVAILABLE: installed PyPI proof, G7.1 closeout, and whole-MVP acceptance. Local OpenSpec and test output are not those proofs.

## Acceptance criteria

- The route step accepts only a child exit of 0, a first stdout line of `200`, and a JSON object whose `decision` is a non-empty string other than `denied`.
- Any other status, a non-zero child, malformed JSON, a missing or empty decision, or `decision` of `denied` exits non-zero and prints `RESULT: FAIL`.
- HTTP 200 with a non-denied decision may continue. API auth, request payload, and lifespan stay unchanged.
- G7.1, the PyPI claim, and whole-MVP closeout remain OPEN. This change does not mark them done.
