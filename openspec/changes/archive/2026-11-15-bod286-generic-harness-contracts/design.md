# Design: generic harness contracts

## Contracts

`ExecutionTarget` is a frozen dataclass with disjoint fields:

- `harness`: registered adapter name (`prime-headless`, `direct-gateway`,
  future adapters, test fakes).
- `transport`, `provider`, `model`, `capacity_class`,
  `reasoning_effort`, `compatibility_profile`: the execution identity the
  eligibility ladder fills at dispatch time. Harness identity never implies
  provider/model identity — enforced by an AST architecture test.

`HarnessCapabilities` enumerates every `HarnessCapability` facet with a
`CapabilityLevel` (`supported` / `partial` / `unsupported` /
`not-installed`). Registration rejects an adapter that omits any facet
(`OmittedCapabilityError`): unsupported capabilities are explicit, never
inferred or silently emulated.

`HarnessAdapter` is a Protocol: `capabilities`, `worker_executor`,
`harness_visibility`, `label`, `not_implemented`. Adapters translate Verdict
decisions into launches; they never rank models, broaden admission, or own
cross-harness orchestration.

`HarnessRegistry` resolves adapters by name (plus aliases), maps executor
classes to receipt labels, and builds executors from an
`ExecutionTarget` via `executor_for`. `DEFAULT_REGISTRY` is the single
production registry.

## Wiring rules

- CLI `--executor` / `--executor-map` names come from
  `DEFAULT_REGISTRY.names()`; the legacy `prime` alias resolves to
  `prime-headless`.
- `build_selector(visibility_executor=...)` resolves the adapter and calls
  `harness_visibility`; the `if direct-gateway / else prime` branch is gone.
- `_selector_visibility_executor` preserves main's rule: a pure
  `direct-gateway` run gets live-gateway visibility; any `--executor-map`
  (mixed dispatch) forces the default adapter's strict visibility.
- `run.py` receipt/event labels come from `DEFAULT_REGISTRY.label_for`.

## Non-goals

No interactive attach, model switching, checkpoint/resume, MCP/hooks or
tool interception is implemented by the Prime or direct-gateway adapters —
those facets are declared `unsupported`/`not-installed`. BOD-287 owns
interactive Prime; future stories register additional harnesses.
