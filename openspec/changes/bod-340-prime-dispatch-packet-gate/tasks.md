# Tasks

Owned files: `scripts/prime_workflow.py` (`validate_dispatch` only),
`.prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py`,
`.prime/agent/skills/verdict-dispatch/SKILL.md`, `tests/test_prime_dispatch_gate.py` and this change.

- [x] 1.1 Add `validate_dispatch` reusing `validate_packet` and `require_lease_for_pr`.
- [x] 1.2 Bridge requires `packet` and `issue_updated_at`, gates before launch, binds identity.
- [x] 1.3 Update the `verdict-dispatch` skill example and refusal semantics.
- [x] 1.4 Focused tests for admit, every refusal reason, bridge launch/refusal and caller coverage.
