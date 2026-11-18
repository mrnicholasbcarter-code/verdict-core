# Proposal: Route clock follow-ups

## Why
The execution-path admission_proof call in verdict/intelligence.py read its own datetime.now(timezone.utc) instead of the request clock IntelligenceService._now, so an injected admit_now was ignored for admission proofs; the admit_now setter checked tzinfo only, so a tzinfo whose utcoffset() returns None was accepted although it compares like a naive datetime.

## What Changes
admission_proof gets now=self._now on the execution path; the setter also rejects utcoffset() None with the same ValueError.

## Capabilities
### New Capabilities
- `route-clock-followups`: execution-path admission proofs use the injected request clock via IntelligenceService._now, and the admit_now setter rejects a tzinfo whose utcoffset() is None.

### Modified Capabilities

## Impact
verdict/intelligence.py and tests/test_route_clock_followups.py only; no API, gate, dependency or install change.
