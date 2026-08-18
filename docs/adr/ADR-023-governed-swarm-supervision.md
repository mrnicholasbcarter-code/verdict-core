# ADR-023: Governed Swarm Supervision

**Status**: Accepted (partially implemented)
**Date**: 2026-08-16
**Story**: [VERDICT-SWARM-001](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/VERDICT-SWARM-001.md) (verdict-ecosystem)

## Context

`verdict/swarm_contracts.py` (Issue #43 / Slice 37.1) already defines bounded,
resumable, measurable task envelopes (`SwarmTaskEnvelope`, `SwarmTaskBudget`)
and lifecycle contracts for lower-tier swarm workers, enforced through
`SwarmDispatcher`. SWARM-001 adds the layer above that: a supervisor that (a)
validates a swarm spec's required capabilities and concurrency against its
envelope before dispatch, and (b) can pause, resume, and query a running
swarm task through a real orchestration backend (Ruflo) rather than only a
fake/mock adapter.

While grounding this story, `RufloAdapter`'s `submit`/`status`/`control`/
`result` methods were found to call `self._transport(...)` as a bare
function — which raises `TypeError` on any real `RufloTransport` instance
(only fake mode and callable-mock transports happened to work, which is what
the existing tests exercised). This would have made SWARM-001's pause/resume
control guarantee false under a real orchestration backend. Fixed as part of
this story's verification, not as an unrelated cleanup — see
`verdict/ruflo_adapter.py`'s `_dispatch()` helper, which now branches on
`isinstance(self._transport, RufloTransport)` and preserves both call
conventions.

## Decision

Add `tests/test_governed_swarm_supervisor.py` pinning two conformance
properties:

- `test_swarm_spec_supervisor_validates_roles_and_envelope_link`: a
  `SwarmDispatcher` constructed with a `SwarmDispatchPolicy` correctly
  derives `max_concurrency`/`max_budget` from the linked `SwarmTaskEnvelope`,
  and exposes the envelope's `required_capabilities` for role validation.
- `test_supervisor_control_delegates_to_ruflo_adapter`: pause/resume control
  requests delegate through the Ruflo adapter's real state machine — pausing
  a task before it reaches `RUNNING` is rejected (`success=False`); pausing
  once `RUNNING` succeeds and transitions to `PAUSED`.

Combined with the `RufloAdapter._dispatch` fix, this makes the pause/resume
control path correct against both the fake adapter (used in tests without a
live backend) and a real `RufloTransport` (subprocess or HTTP).

## Consequences

- Swarm control commands (pause/resume/status) are now exercised against the
  adapter's real dispatch path, not only the fake/mock path — closing a gap
  that would otherwise only surface at first production use.
- This ADR covers spec-to-envelope validation and control delegation, not
  the full SWARM-001 surface: end-to-end multi-agent budget enforcement
  under a live Ruflo backend and worker-side capability attestation remain
  open, tracked separately.
- `RufloAdapter._dispatch`'s dual-mode contract (RufloTransport vs. bare
  callable) is now an explicit, tested seam — any new transport type must
  satisfy one of the two shapes or extend `_dispatch` deliberately.
