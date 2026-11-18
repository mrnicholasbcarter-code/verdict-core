# Tasks

## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta with scenarios
- [x] 1.2 `openspec validate inband-credit-refusal --strict` passes

## 2. Tests first

- [x] 2.1 `tests/test_inband_credit_refusal.py`: detector, probe exchanges, probe results, relay failover, relay 402, passive refresh, serve check
- [x] 2.2 The tests fail before the fix and pass after it

## 3. Implementation

- [x] 3.1 Shared detector `is_inband_billing_refusal` and two more not-free phrases
- [x] 3.2 Probe transports return a failed `payment_required` exchange; the result is route-scoped
- [x] 3.3 Relay counts a buffered refusal as a failed attempt, records route-scoped payment feedback and tries the next candidate
- [x] 3.4 Passive refresh skips zero-output completions and keeps a billing negative
- [x] 3.5 Acceptance serve check fails on a refusal body
- [x] 3.6 `docs/CONFIGURATION.md` describes the passive rule

## 4. Gates

- [x] 4.1 Focused tests, ruff check, ruff format, mypy --strict
- [x] 4.2 Doc links, reachability ratchet, lint gates

## 5. Live verification

- [ ] (post-merge) 5.1 Install merged main and confirm the six `pol/*` routes are no longer TASK_ELIGIBLE and `verdict acceptance run` is green
