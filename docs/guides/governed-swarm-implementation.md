# Governed Swarm Implementation

## Source snapshot

Feature 274 is implemented in the isolated worktree `/home/nick/dev/verdict-core/.worktrees/274-swarm-001` on branch `feat/274-swarm-001`, based on immutable source commit `4a206dae869f6a201e887e247f1a63fea72b5764`.

The dirty `/home/nick/dev/verdict-core` main checkout is not used for implementation or validation. Validation evidence must identify the current feature-worktree commit, or the exact dirty-worktree diff when work has not yet been committed.

## Contract boundaries

`verdict.swarm_governance` owns the portable `swarm-spec/v1` policy contract, deterministic validation, canonical identity, effective bounds, conflict decisions, and immutable envelope linkage. It does not import Ruflo types.

`verdict.swarm_runtime` owns the portable `swarm-runtime/v1` lifecycle protocol and normalized structured failures. Core policy remains authoritative when a runtime reports broader bounds, invalid transitions, unverified success, or out-of-scope evidence.

`verdict.swarm_supervisor` applies legal lifecycle controls and narrowing-only deadlines through the portable runtime protocol. `status` and `result` are non-mutating operations; `pause`, `resume`, and `cancel` are the only controls.

`verdict.swarm_evidence` maps governed mission events to the existing append-only `ReceiptStore`. Evidence contains allowlisted metadata, opaque references, and digests—not raw prompts, completions, messages, commands, tool arguments, credentials, or sensitive URLs. Offline replay verifies integrity and never executes work.

`verdict.ruflo_adapter` remains the runtime-specific boundary. Its governed-runtime bridge maps portable requests to existing Ruflo requests and responses but cannot mutate Core policy or expand approved envelopes.

## Compatibility and limitations

Existing `SwarmTaskEnvelope`, `SwarmDispatchPolicy`, `SwarmDispatcher`, `ReceiptStore`, and `RufloAdapter` behavior remains compatible. The feature extends those seams rather than replacing them. Live Ruflo execution is optional evidence; deterministic conformance uses the real adapter over a fake transport and is the required gate.

`swarm-spec/v1` does not support replanning. Authentication of the already-authorized supervisor and orchestration-backend implementation are outside this feature.

## SwarmSpec validation

Pre-dispatch validation rejects unknown versions, unknown fields, missing required fields, empty/duplicate/unresolved identifiers, malformed references, unsafe content, capability overlap, assignments outside grants, invalid model constraints, missing verification, non-positive/non-finite/over-limit budgets, invalid timeout/iteration/concurrency bounds, and a missing or mismatched envelope digest. Errors expose a stable code, JSON-style field path, redacted reason, and swarm/correlation identity. Identical versioned input produces the same digest.

Every executable slice must capture an immutable canonical envelope digest via `verdict.swarm_contracts.capture_envelope_digest` / `validate_envelope_link`. Missing digests, digest mismatches, and weakened bounds (higher concurrency/timeout/budget/attempts, or expanded paths/capabilities) fail closed before dispatch. `dispatch_governed_swarm` validates the `SwarmSpec` before any dispatcher or adapter call. Effective dispatcher limits are the minimum across swarm, role, slice/envelope, and dispatcher policy (`SwarmDispatchPolicy.from_swarm_bounds`).

Serialized example:

```json
{
  "schema_version": "swarm-spec/v1",
  "swarm_id": "swarm-1",
  "objective": "ship governed swarm contract models",
  "roles": [
    {
      "role_id": "coder",
      "name": "Coder",
      "required_capabilities": ["edit"],
      "optional_capabilities": ["test"],
      "forbidden_capabilities": ["deploy"],
      "allowed_tools": ["read_file", "write"],
      "resource_refs": [],
      "model_floor": "low",
      "model_allowlist": [],
      "max_parallelism": 1
    }
  ],
  "agents": [
    {
      "agent_id": "agent-1",
      "role_id": "coder",
      "capabilities": ["edit", "test"],
      "allowed_tools": ["read_file"],
      "resource_refs": ["/home/nick/dev/verdict-core/verdict/swarm_governance.py"],
      "model": "low",
      "slice_id": "slice-1"
    }
  ],
  "context_refs": ["context-pack:abc"],
  "model_constraints": {"allowlist": ["low", "mid"]},
  "budget": {"max_usd": 0.0, "max_tokens": 2000, "max_latency_ms": 0},
  "max_concurrency": 1,
  "conflict_policy": {
    "policy_id": "conflict",
    "version": "1",
    "strategy": "priority_then_digest",
    "tie_break": "lexical_digest"
  },
  "supervisor": {
    "allowed_controls": ["pause", "resume", "cancel"],
    "cancellation_deadline_ms": 1000,
    "max_control_retries": 0,
    "terminal_policy": "first_valid_terminal_wins"
  },
  "verification": {
    "profile_id": "verify-core",
    "version": "1",
    "required_checks": ["pytest"],
    "required_evidence": ["test-report"],
    "verification_command": null,
    "fail_closed": true
  },
  "evidence_scope": "swarm/scope"
}
```

## Supervisor lifecycle and narrowing

`status` and `result` are observations. `pause`, `resume`, and `cancel` are the only supervisor controls. Cancellation uses an injected monotonic clock: if acknowledgement is absent at the deadline, Core emits a structured `timeout`/`cancellation` failure with zero scheduling tolerance. Runtime responses that observe broader bounds, substitute an envelope, or skip required verification fail closed and cannot mutate policy.

## Evidence, redaction, and replay

`MissionEvidence` records allowlisted metadata, digests, and references on `ReceiptStore`. Credentials, prompts, completions, messages, tool arguments, and sensitive URLs are rejected or redacted before persistence. Offline replay verifies integrity and projects lifecycle, conflict, and terminal facts without executing work, calling a model, or granting authority. Conflict selection is `priority_then_digest` with `lexical_digest` tie-break.
