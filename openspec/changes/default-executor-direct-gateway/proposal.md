# default-executor-direct-gateway: Verdict works without Prime Agent

## Why

For 1.0, Verdict must work on a machine that has only a gateway. Today
`verdict orchestrate` defaults to the `prime-headless` executor, plain
`verdict eligibility` reads Prime's model registry to decide which routes
exist, `verdict setup` plans a Prime registry, `verdict doctor` fails when the
registry or the prime-sync timer is missing, and live acceptance has a required
`harness-prime` check. A host without the Prime Agent binary therefore fails
every worker (`spawn failed`), shows red doctor rows and a red acceptance job.
On a host with Prime, newly added gateway routes were `not_harness_visible`
for up to 15 minutes until `verdict-prime-sync.timer` rewrote the registry.
The direct-gateway executor is proven live (run 20261119T000727Z).

## What Changes

- The registry default worker executor becomes `direct-gateway`. `--executor`
  defaults to it; any internal fallback uses the registry default, never Prime.
  `prime-headless` and its alias `prime` stay selectable and unchanged.
- `build_selector`'s default visibility follows the registry default: the live
  gateway catalog. Prime visibility applies when Prime is the executor, when an
  `--executor-map` sends any node to Prime, or with `--sync-visibility`.
- Live acceptance: `harness-prime` is skipped (never passed, never failed) with
  the reason `Prime Agent not installed (optional harness)` when the Prime
  binary is absent or its Verdict harness is not enabled. Otherwise it runs as
  before.
- Setup plans no `host.prime-registry` action without the Prime binary.
  Doctor reports the Prime registry and a missing prime-sync timer as
  `not-installed` (optional, not an issue) without the Prime binary.

**Behaviour change for existing users:** anyone who relied on Prime implicitly
(no `--executor` flag) now gets `direct-gateway`. To keep Prime, pass
`--executor prime-headless` (or `--executor prime`).

## Safety

- Admission and ranking are unchanged; only the default harness and its
  visibility source change. Opaque routes stay excluded by both visibility
  adapters.
- A skipped optional check is never reported as passed, and a skipped check
  never counts toward green unless it is skipped for absence.
- Tests stub the Prime binary lookup and registry; none read `~/.prime` or call
  a provider.

## Capabilities

### Modified Capabilities
- `generic-harness-contracts`: default executor and visibility.
- `setup-host-prerequisites`: Prime registry is optional.

## Impact

`verdict/orchestration/{harness,cli,eligibility_report}.py`,
`verdict/actions/registry.py`, `verdict/acceptance.py`,
`verdict/acceptance_state.py`, `verdict/cli.py`, `verdict/setup_host.py`,
`verdict/doctor_diagnostics.py`, docs/CLI_REFERENCE.md, docs/CONFIGURATION.md.

## Acceptance Criteria

- `verdict orchestrate` without `--executor` builds the direct-gateway executor; `--executor prime-headless` and `--executor prime` build Prime.
- Plain eligibility sees a live route that Prime's registry lacks; Prime selection refuses it.
- Without Prime, `harness-prime` is skipped (not passed, not failed), setup plans no Prime registry, and doctor shows Prime rows `not-installed` without an issue.
- With Prime installed and enabled, all three behave as before.

## Evidence Boundary

- **KNOWN**: `--executor` defaulted to `prime-headless` and the registry registered Prime with `default=True` on main c4e8349e; a live `verdict orchestrate --executor direct-gateway` run completed (run 20261119T000727Z); routes added to the gateway showed `not_harness_visible` until the prime-sync timer ran.
- **INFERRED**: Without the prime-agent binary every default worker ended `spawn failed` (read from `PrimeHeadlessExecutor`, not run on a Prime-less host).
- **NOT AVAILABLE**: A live run on a host with no Prime binary; it is a post-merge task.
