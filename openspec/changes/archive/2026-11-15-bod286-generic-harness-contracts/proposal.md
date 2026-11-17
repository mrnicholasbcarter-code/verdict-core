# Generic harness contracts

## Why

Verdict's orchestration layer selected worker executors through scattered
`prime` / `direct-gateway` string branches in the CLI, the eligibility
selector, `MixedExecutor` and the receipt label table. Adding a second coding
harness (Claude Code, OpenCode, ...) would have required touching every one of
those sites and risked conflating harness identity with provider/model
identity. BOD-286 extracts the harness-neutral contracts first so Core stops
depending on Prime-specific execution assumptions before any new harness is
added.

## What Changes

- New `verdict/orchestration/harness.py` (schema `verdict.harness/v1`):
  `ExecutionTarget` (harness / transport / provider / model / capacity_class /
  reasoning_effort / compatibility_profile as disjoint fields),
  `HarnessCapability` + `CapabilityLevel`, `HarnessCapabilities`,
  `HarnessAdapter` protocol, `HarnessRegistry`, and `DEFAULT_REGISTRY`.
- `DEFAULT_REGISTRY` registers `PrimeHeadlessAdapter` (name
  `prime-headless`, alias `prime`, default) and `DirectGatewayAdapter`
  (`direct-gateway`), plus receipt labels for the FaultInjecting / Mixed /
  Scripted wrappers.
- `cli.py`: `--executor` choices and `--executor-map` backends resolve
  through the registry; `_build_single_executor` builds via
  `registry.executor_for(ExecutionTarget(harness=name))`;
  `_selector_visibility_executor` keeps main's rule that any executor map
  forces the strict default-adapter visibility.
- `eligibility_report.py`: `build_selector` delegates
  `visibility_executor` to `registry.resolve(name).harness_visibility(...)`.
- `run.py`: executor identity labels come from
  `DEFAULT_REGISTRY.label_for(executor)`.
- `executors.py`: `MixedExecutor._BACKEND_KINDS` removed; the registry owns
  backend names.
- Prime behavior is unchanged: same executor classes, same `harness` labels
  on terminals and receipts, same admission/visibility semantics.

## Capabilities

### New Capabilities

- `generic-harness-contracts`: versioned execution-target and adapter
  contracts with a registry that resolves harness names without
  harness-specific branching in Core.

## Impact

`verdict/orchestration/harness.py` (new), `cli.py`, `eligibility_report.py`,
`executors.py`, `run.py`, and focused tests. No dispatch, admission,
context-pack, budget, receipt-integrity or OmniRoute changes. Interactive
harness sessions remain out of scope (owned by BOD-287); no new harness is
registered by this change.
