# OC1: OpenCode worker adapter — default orchestrate worker

> **Scope (2026-11-24):** this change covers the `verdict orchestrate` default worker
> only. The `verdict autodev` model-tier rung moved to issue #1301 (OC2), which will
> carry its own OpenSpec change.

## Why

`verdict orchestrate` coding nodes today use `direct-gateway`: a single API
call to OmniRoute that returns text. The worker cannot run its own tests or
lint after editing files. This is why `verdict deliver` runs (#1269) were
BLOCKED: one-shot workers produce edits but cannot verify them, so the
post-attempt verification step always fails.

OpenCode runs a full edit–test–fix loop inside a repository before returning.
Switching the default worker executor from `direct-gateway` to `opencode` is
the minimum change needed to unblock #1269 and enable Verdict to build its own
pipeline stories through `verdict orchestrate`.

This directly advances the mission
([BOD-342 D15](https://linear.app/bodanglin/issue/BOD-342)): every story Verdict
builds itself with a free worker is work at no paid cost. OpenCode workers will
run on the cheapest qualified route Verdict can admit.

## What Changes

- A new `OpenCodeAdapter` is registered in `DEFAULT_REGISTRY` and becomes the
  default worker executor for `verdict orchestrate`, replacing `direct-gateway`
  as the default. `direct-gateway` and `prime-headless` stay registered and
  selectable. The planner executor (`--planner-executor`, answer-only,
  `direct-gateway`) is unchanged.
- A new `OpenCodeExecutor` runs `opencode run` in non-interactive mode
  (`--auto --standalone --format json`), passing the Verdict-chosen route via
  `--model verdict/<route_id>` and routing all model calls through
  `verdict serve` (:8000). Each attempt writes a per-attempt OpenCode config
  into a temp directory (`OPENCODE_CONFIG_DIR`), isolating session DB and global
  state between attempts and concurrent workers.
- The adapter declares all nine `HarnessCapability` facets explicitly, as
  required by ADR-041.
- OpenCode is pinned at v2.3.2. `verdict doctor` warns when the installed
  version differs from the pinned version.

## Capabilities

### New Capabilities

- `opencode` harness adapter and executor: `worker_launch` SUPPORTED, edits
  inside node worktree, routes through serve, isolated per-attempt config.
- Doctor version check: warns when the installed OpenCode version differs from
  the pinned v2.3.2.

## Impact

- **Existing orchestrate runs** that do not pass `--executor` will use
  `opencode` instead of `direct-gateway` once the adapter merges. Users who
  depend on `direct-gateway` for coding nodes must pass `--executor
  direct-gateway` explicitly. `prime-headless` users are unchanged.
- **`verdict serve` gains a per-request route-pinning requirement** so the
  relay can enforce exactly the route Verdict chose. This is an implementation
  detail of the build (open point: serve today does not pin per request; adding
  that is scoped to this story).
- **Shell commands inside OpenCode workers are not restricted to an
  allow-list**; this is the operator's explicit choice. Post-attempt owned-files
  enforcement and node verification remain the enforcement layer.

## Evidence boundary

- **KNOWN:** `HarnessAdapter`, `HarnessRegistry`, `HarnessCapability` facets
  and `CapabilityLevel` values were read from `verdict/orchestration/harness.py`.
  `WorkerTerminal` fields were read from `verdict/orchestration/contracts.py`.
  `PrimeHeadlessExecutor` command shape was read from
  `verdict/orchestration/executors.py`. Existing orchestrate bounds (900 s
  timeout, idle-output kill via `VERDICT_WORKER_IDLE_SECONDS`, 4
  attempts-per-node default) were confirmed in `verdict/orchestration/cli.py`,
  `runtime.py` and `recovery.py`. `DEFAULT_REGISTRY` registers
  `DirectGatewayAdapter(default=True)` (confirmed `harness.py`). The
  `default-executor-direct-gateway` OpenSpec change made `direct-gateway` the
  code default after ADR-041.
- **KNOWN:** `opencode run --auto --standalone --format json --model
  provider/model#variant --session <id> --archived` is a supported
  non-interactive invocation (opencode v2.3.2 `--help`, verified by the
  controller). `OPENCODE_CONFIG_DIR` points a process at a config directory; this adapter
  uses that form (see design.md, deviation note).
  `--standalone` runs a private server per invocation, isolating the session DB.
- **KNOWN:** `verdict serve` (:8000) today does not pin a specific route for each
  individual worker request. Building per-request pinning is scoped to this
  story. This is an open point explicitly recorded in ADR-053.
- **INFERRED:** `opencode run --format json` emits structured JSON that includes
  model identity and usage fields sufficient to populate `WorkerTerminal`; the
  exact field names are implementation details to confirm during the build.
- **NOT AVAILABLE:** a live end-to-end orchestrate run on OpenCode workers on
  the operator's gateway. The post-merge task covers this.

## Acceptance criteria

- `verdict orchestrate` with no `--executor` flag dispatches coding nodes to
  an OpenCode worker; the receipt shows `harness: opencode`.
- The OpenCode worker's model calls reach `verdict serve` (:8000), not
  OmniRoute (:20128) directly; the relay records the attempt with the exact
  chosen route.
- An attempt that runs past the 900 s timeout is killed; an attempt whose
  output stalls past `VERDICT_WORKER_IDLE_SECONDS` is killed. Both are recorded
  as failures, not hangs.
- A worker that edits a file outside its node worktree is rejected (Verdict
  owned-files check), and the attempt is classified as a failure.
- `verdict doctor` warns (non-fatal) when the installed OpenCode version differs
  from the pinned v2.3.2.
- Concurrent workers on separate nodes do not share session state.
- `direct-gateway` and `prime-headless` remain selectable via `--executor`.

## GitHub issue

Tracked in [#1277](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1277).
