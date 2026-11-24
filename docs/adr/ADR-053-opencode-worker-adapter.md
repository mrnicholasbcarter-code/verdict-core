# ADR-053: OpenCode worker adapter

**Status:** Accepted — every decision agreed with the operator (2026-11-23, two rounds); not yet implemented
**Date:** 2026-11-23
**Deciders:** Operator
**Amends:** [ADR-041](ADR-041-generic-harness-contracts.md) — changes the default worker executor for `verdict orchestrate`
**Relates to:**
[ADR-036](ADR-036-goal-to-receipt-orchestration.md) (goal-to-receipt orchestration),
[ADR-041](ADR-041-generic-harness-contracts.md) (generic harness contracts),
[ADR-049](ADR-049-cost-policy-and-unknown-handling.md) (cost policy and unknown handling),
[ADR-012](ADR-012-consented-budgeted-probes.md) (consented, budgeted probes)

## Context

`verdict orchestrate` dispatches coding work through the `HarnessAdapter` /
`HarnessRegistry` contract introduced in ADR-041. ADR-041 set `prime-headless`
as the initial default; the `default-executor-direct-gateway` OpenSpec change
later moved the code default to `direct-gateway` (confirmed: `harness.py`
registers `DirectGatewayAdapter(default=True)`). The `prime-headless` and
`direct-gateway` adapters are both answer-only: they dispatch a single
API call to OmniRoute and return. They cannot run tests, linters or repo gates
after editing files. This is why `verdict deliver` runs (#1269) were BLOCKED:
one-shot workers produce edits but cannot run their own tests and lint, so the
verification step that follows always fails.

OpenCode (opencode.ai) is an agentic coding tool that runs a full edit–test–fix
loop inside a repository before returning. It supports non-interactive dispatch
(`opencode run --auto --standalone`), per-process configuration via
`OPENCODE_CONFIG_DIR` (a directory) or `OPENCODE_CONFIG` (a file path), and a
provider interface that accepts any OpenAI-compatible endpoint. These properties
make it a natural fit for coding nodes in `verdict orchestrate` and for the
model-tier coding rung in `verdict autodev`.

*Implementation note (2026-11-24): the adapter isolates each attempt with
`OPENCODE_CONFIG_DIR` (a per-attempt temp directory). `OPENCODE_CONFIG` (a single file)
exists in v2.3.2 but is not used. The autodev rung named above is delivered separately,
in #1301 (OC2).*

Research findings and decision points were gathered on 2026-11-23 and agreed
with the operator in two rounds before this ADR was written.

**Open point:** `verdict serve` (:8000) today does not pin a specific route for
every individual request from a worker. Building that per-request pinning is
part of the adapter implementation work (Decision 1 below). This ADR records
the agreed architecture; the implementation detail is in the OpenSpec change.

## Decision

The eight decisions below were agreed with the operator on 2026-11-23 in two
rounds and are recorded exactly as agreed.

**1. Route through Verdict serve, not direct OmniRoute.**
Workers' model calls go through `verdict serve` (:8000), never straight to
OmniRoute (:20128). Each request must use exactly the route Verdict chose; the
relay pins it and records the attempt, and admission and cost rules still apply.
If serve cannot pin a route per request today, adding that is part of the build.

*Implementation note (2026-11-24): pinning is done inside admission, not by the relay. The relay
passes the worker's assigned route to admission as a typed input (`WorkerAssignedRoute`, set only
when the `X-Verdict-Worker` header is present). Admission selects that route only when that exact
id is in the request's admitted set, mints its route-bound proof exactly as for a chosen route
(ADR-035), and otherwise denies without minting a proof. The relay only verifies the outcome. The
decision above is unchanged.*

*Implementation note (2026-11-24): the worker authenticates to serve with serve's own bearer
token, resolved by Verdict (`verdict.serve_service.resolve_serve_token`), passed to the child only
as `VERDICT_WORKER_API_KEY`, and checked once before launch together with the binary and serve's
reachability. Until issue #1313 (OC1b) the route is pinned by the worker header: a worker could
name another admitted route, never a non-admitted one. #1313 replaces the credential with a signed
per-attempt assignment.*

**2. OpenCode becomes the default orchestrate worker executor.**
`opencode` becomes the DEFAULT worker executor for `verdict orchestrate` as soon
as the adapter merges. The planner executor is unchanged: `--planner-executor`,
answer-only, `direct-gateway`. `direct-gateway` and `prime-headless` stay
registered. This amends ADR-041's default (prime-headless).

**3. Autodev: OpenCode as the model-tier coding executor.**
OpenCode is added as the coding executor for model-tier units in
`verdict autodev`. PatchExecutor is kept for mechanical units, together with its
diff-path containment check.

*Delivery note (2026-11-24): the autodev rung is delivered separately from the orchestrate
default. It moved to issue #1301 (OC2), which carries its own OpenSpec change, after
cross-family review found isolation and retry defects. The decision above is unchanged.*

**4. Sandbox: worktree-scoped edits; shell commands not restricted.**
An OpenCode worker may edit only inside its node worktree. Edits elsewhere and
external directories are denied in its per-attempt OpenCode config. Shell
commands are NOT restricted to an allow-list; this is the operator's explicit
choice. Verdict still enforces owned files afterwards (a diff outside owned
files is rejected), and runs the node's verification and the repo gates itself.
OpenCode's own checks are advisory.

*Accepted risk (2026-11-24, operator): the worker keeps the operator's home directory, so it
can read files there and reach the gateway directly; "all calls go through serve" holds for
OpenCode's own provider calls only. Restricting the shell would reverse this decision and
needs its own design.*

**5. Licence and terms; pinned version; doctor warning.**
The first build task records OpenCode's licence and terms, taken from its
repository and site, into this ADR (see "Licence and terms" section below).
OpenCode is pinned at v2.3.2. `verdict doctor` warns when the installed version
differs from the pinned one. Any upgrade must pass the adapter's live check
again.

**6. Bounds: existing limits apply; no new token cap.**
The existing per-attempt timeout (default and maximum 900 s, confirmed:
`orchestration/cli.py` default 900, `runtime.py` `attempt_timeout_seconds`
900.0), the idle-output stall kill (`VERDICT_WORKER_IDLE_SECONDS`,
`orchestration/executors.py`), and the attempts-per-node budget (default 4,
`recovery.py` `max_attempts_per_node`) all apply to OpenCode workers unchanged.
No new token cap is added.

**7. Build order.**
The adapter comes first. Next, re-run #1269 (deliver) on OpenCode workers. Then
#1276 (gate environment) and #1274 (repo map).

**8. Builder and review.**
A Claude subagent builds the adapter from this spec, with a non-Claude
cross-family review. After that, Verdict builds the later stories on OpenCode
workers.

## Licence and terms

**SPDX licence:** MIT

**Repository licence:** <https://github.com/sst/opencode/blob/v2.3.2/LICENSE>
(Copyright 2025 OpenCode)

**Canonical repository:** <https://github.com/anomalyco/opencode>
(`sst/opencode` hosted v2.3.2; the ToS page links to `anomalyco/opencode` as the
current canonical location.)

**Terms of service:** <https://opencode.ai/legal/terms-of-service>
(effective 8 November 2026)

**Date checked:** 2026-11-23

**Usage restrictions relevant to automated agentic use:**

The OpenCode ToS covers the hosted opencode.ai service only. The ToS states:

> "our open source software that is not provided to you on a hosted basis is
> subject to the open source license and terms set forth on the applicable
> repository … and such license and terms will exclusively govern your use of
> such open source software."

Running `opencode run --standalone` locally, with model calls routed through
Verdict serve (:8000) rather than opencode.ai's hosted inference, is covered by
the MIT licence alone. The hosted-service restrictions (including the prohibition
on automated data extraction from their service output) do not apply to the
self-run binary.

**Conclusion:** MIT permits automated agentic use of the self-run, non-hosted
binary. No ToS restrictions apply. Proceed.

## Consequences

- `verdict orchestrate` coding nodes run a full edit–test–fix loop, which
  resolves the core reason #1269 (deliver) runs were BLOCKED: one-shot workers
  could not run their own tests and lint.
- The relay gains a per-request route-pinning requirement. Until that is built,
  the serve relay forwards the admission decision but cannot enforce per-call
  route identity; the adapter implementation covers this.
- Shell commands inside the worker are not sandboxed to an allow-list (Decision
  4). Verdict's post-attempt owned-files check and verification commands remain
  the enforcement layer. The risk is accepted explicitly by the operator.
- OpenCode's dependency on the background opencode service is bypassed by
  `--standalone`. State (session DB, config) is isolated per attempt via
  `OPENCODE_CONFIG_DIR` pointing at a temp directory (the directory form is
  what OpenCode v2.3.2 reads for per-attempt isolation).
- PatchExecutor and its diff-path containment check are preserved for mechanical
  autodev units, so the fail-closed behaviour for diff-only work is unchanged.
- The adapter's capability declaration (`HarnessCapabilities`) is part of the
  build and must list every facet explicitly as required by ADR-041.
- Upgrading OpenCode past v2.3.2 requires a re-run of the adapter's live check
  (Decision 5), keeping the pinned version honest.
