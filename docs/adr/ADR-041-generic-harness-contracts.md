# ADR-041: Generic harness contracts for orchestration

Status: Accepted (implemented in PR #1025, merged as `e1433271`)
Date: 2026-11-15
Supersedes: none; extends ADR-036 (goal-to-receipt orchestration) with the
adapter boundary described below.
Related: ADR-035 (authorized selected-route dispatch — model/route authority
stays with the eligibility ladder), ADR-032 (Core model metadata store /
OmniRoute transport authority), BOD-271/BOD-104 (route authority),
BOD-272/BOD-125 (context authority), BOD-119 (session economics).

## Context

Verdict orchestrates bounded work nodes through `WorkerExecutor`
implementations. Before this change, harness selection was hard-coded:
`--executor` accepted only `prime`/`direct-gateway`, `_build_single_executor`
branched on the name, `build_selector` branched `visibility_executor` on
`"direct-gateway"`, `MixedExecutor` kept its own backend-name map, and
`run.py` kept a class-name → label table. Adding a second coding harness
would have scattered harness-specific conditionals across Core and risked
conflating "which harness runs the node" with "which provider/model/route
serves it".

## Decision

Introduce a small, versioned contract layer in
`verdict/orchestration/harness.py` (`verdict.harness/v1`):

- `ExecutionTarget` — frozen dataclass with disjoint `harness`, `transport`,
  `provider`, `model`, `capacity_class`, `reasoning_effort`,
  `compatibility_profile` fields. Harness identity never implies
  provider/model identity (enforced by an AST test).
- `HarnessCapabilities` / `HarnessCapability` / `CapabilityLevel` — every
  facet (`worker_launch`, `interactive_attach`, `model_switching`,
  `context_injection`, `resume_checkpoint`, `mcp`, `hooks`,
  `tool_interception`, `event_stream`) is declared explicitly; registration
  rejects adapters that omit any facet.
- `HarnessAdapter` protocol — `capabilities`, `worker_executor`,
  `harness_visibility`, `label`, `not_implemented`. Adapters translate
  Verdict decisions into launches; they never rank models, broaden
  admission, or own cross-harness orchestration.
- `HarnessRegistry` / `DEFAULT_REGISTRY` — the single place that maps a
  harness name (or alias) to an adapter, builds executors from an
  `ExecutionTarget`, and maps executor classes to receipt labels.

`DEFAULT_REGISTRY` registers `PrimeHeadlessAdapter` (`prime-headless`,
default, alias `prime`) and `DirectGatewayAdapter` (`direct-gateway`). The
CLI, eligibility selector and receipt labeling all resolve through the
registry; the Prime-specific string branches are removed.

## Consequences

- Prime autonomous dispatch is behaviorally unchanged: same executor
  classes, same `harness` labels on terminals/receipts, same admission and
  visibility semantics. The `--executor-map` rule (any map forces the
  strict default-adapter visibility) is preserved.
- Unsupported capabilities are explicit (`unsupported`/`not-installed`),
  never silently emulated. Interactive attach, model switching,
  checkpoint/resume, MCP/hooks and tool interception are declared
  unsupported for both shipped adapters.
- Interactive Prime sessions and additional harnesses (Claude Code,
  OpenCode, ...) are out of scope: BOD-287 owns interactive Prime, future
  stories register new adapters. No new harness-specific routing or context
  policy is introduced by this change.
- Route/model authority stays with BOD-271/BOD-104; context authority with
  BOD-272/BOD-125; session economics with BOD-119. Adapters consume those
  decisions; they do not own them.
