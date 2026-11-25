# OC1: OpenCode worker adapter — opt-in orchestrate worker

> **Scope (2026-11-24):** this change covers the `verdict orchestrate` worker executor
> only. The `verdict autodev` model-tier rung moved to issue #1301 (OC2), which will
> carry its own OpenSpec change.
>
> **Opt-in, not the default (operator decision, 2026-11-24, "do recommended for all"):**
> the adapter merges selectable with `--executor opencode`; `direct-gateway` stays the
> default. The live smoke showed that `verdict serve` refuses every OpenCode worker request
> before any upstream call (11 of 13 by the escalation keyword scanner, 2 by the capability
> gate without streaming and tools metadata on the relay path), so a default worker would
> burn each node's attempts. The serve fix is issue #1313 (with the signed per-attempt
> assignment); OpenCode becomes the default only after a live smoke passes with it. This
> amends ADR-053 Decision 2.
>
> **Update (2026-11-25):** serve honours a signed worker assignment (#1335) and the executor
> signs one per attempt and gives it to the worker as its only credential (#1325). One live
> attempt through a spare serve from that branch landed an edit on the pinned route (task 2.19).
> The default stays `direct-gateway` until its own change (#1328); the run on installed main is
> post-merge (tasks 2.20 and 8.1).

## Why

`verdict orchestrate` coding nodes today use `direct-gateway`: a single API
call to OmniRoute that returns text. The worker cannot run its own tests or
lint after editing files. This is why `verdict deliver` runs (#1269) were
BLOCKED: one-shot workers produce edits but cannot verify them, so the
post-attempt verification step always fails.

OpenCode runs a full edit–test–fix loop inside a repository before returning.
Making `opencode` a selectable worker executor (`--executor opencode`) is the
minimum change needed to unblock #1269 and enable Verdict to build its own
pipeline stories through `verdict orchestrate`, now that serve admits the worker's
requests (serve's side in #1335, the executor's in #1325). The default worker executor stays
`direct-gateway` until the default is switched in its own change (#1328).

This directly advances the mission
([BOD-342 D15](https://linear.app/bodanglin/issue/BOD-342)): every story Verdict
builds itself with a free worker is work at no paid cost. OpenCode workers will
run on the cheapest qualified route Verdict can admit.

## What Changes

- A new `OpenCodeAdapter` is registered in `DEFAULT_REGISTRY` as an opt-in worker
  executor for `verdict orchestrate`, selected with `--executor opencode`.
  `direct-gateway` remains the default; `direct-gateway` and `prime-headless` stay
  registered and selectable. The planner executor (`--planner-executor`,
  answer-only, `direct-gateway`) is unchanged.
- A new `OpenCodeExecutor` runs `opencode run` in non-interactive mode
  (`--auto --standalone --format json`), passing the Verdict-chosen route via
  `--model verdict/<route_id>` and routing all model calls through
  `verdict serve` (:8000). Each attempt writes a per-attempt OpenCode config
  into a temp directory (`OPENCODE_CONFIG_DIR`), isolating session DB and global
  state between attempts and concurrent workers.
- The executor signs one worker assignment per attempt (route, run id, node id, issued-at,
  expiry: the attempt's timeout plus a 60 s margin, never more than 1,800 s) and gives it to the
  worker as its only credential for `verdict serve`. The runtime carries the run id and node id to
  the executor. The general serve token stays in the orchestrator's process, for one readiness
  check, and never enters the worker's environment, arguments, config or logs. The streaming
  waiver that serve records for an assigned request is read back from serve's receipts and shown
  in the run receipt.
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

- **Existing orchestrate runs** are unchanged: a run that does not pass `--executor`
  keeps using `direct-gateway`, and `prime-headless` users are unchanged. A run opts
  in with `--executor opencode` (or `--executor-map node=opencode`). An opted-in run needs the
  signing key that `verdict setup --apply --allow host.serve` creates, and the orchestrator and
  serve must read the same one: without a readable key, or without a run id and a node id, an
  attempt ends in a `harness_configuration:` block before anything is launched, and the serve
  token is never used instead.
- **A 401 or 403 from the worker is read by where it came from.** Serve refuses a bad assignment
  with 403 `authentication failed`, and forwards a provider's own 401 or 403 unchanged, so the
  words can come from either. The attempt is a configuration block (no cooldown) only when
  serve's receipts, read after the worker is gone, show that serve recorded no request under the
  assignment, in a database shown to be the serve the worker called (a request of an earlier
  attempt of the run was found in it); otherwise, or when the receipts cannot be read, recovery
  sees the provider's denial as before, and a run's first refusal is never proven.
- **`verdict serve` pins each worker request to the route Verdict chose**, from the verified
  signed assignment (serve's side: `serve-worker-assignment`, #1335; the executor signs it:
  #1325). A request whose `model` differs from the signed route is refused before routing.
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
- **KNOWN (2026-11-25):** `verdict serve` (:8000) pins a worker request to the route in its
  verified signed assignment (`verdict/api.py`, `verdict/worker_assignment.py`; #1335), and the
  executor signs one per attempt (`verdict/orchestration/executor_opencode.py`; #1325). At the
  time of the first draft this was an open point recorded in ADR-053.
- **INFERRED:** `opencode run --format json` emits structured JSON that includes
  model identity and usage fields sufficient to populate `WorkerTerminal`; the
  exact field names are implementation details to confirm during the build.
- **NOT AVAILABLE:** a live end-to-end `verdict orchestrate` run on OpenCode workers on installed
  main (post-merge task 8.1). One attempt through `OpenCodeExecutor` against a spare serve from
  the #1325 branch landed an edit on 2026-11-25 (task 2.19).

## Acceptance criteria

- `verdict orchestrate` with no `--executor` flag still dispatches coding nodes to
  `direct-gateway`; `verdict orchestrate --executor opencode` dispatches them to an
  OpenCode worker and the receipt shows `harness: opencode`. (The live check of the
  second is post-merge, on installed main; a spare-serve run of one attempt passed on
  2026-11-25.)
- The OpenCode worker's only credential for `verdict serve` is a signed per-attempt assignment
  (route, run id, node id, issued-at, expiry); the general serve token is absent from the
  worker's environment, arguments, config files and logs. An attempt with no readable signing
  key, no run id or node id, or a timeout whose lifetime would exceed 1,800 s fails closed
  before launch, with no request to serve and no fallback to the serve token.
- The streaming waiver that serve records for an assigned request is shown in the run receipt.
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
