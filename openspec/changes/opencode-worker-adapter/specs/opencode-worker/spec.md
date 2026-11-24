# Spec Delta

## ADDED Requirements

### Requirement: OpenCode adapter is registered as the default worker executor

`DEFAULT_REGISTRY` SHALL register an `OpenCodeAdapter` as the default worker
executor for `verdict orchestrate`. All nine `HarnessCapability` facets SHALL
be declared explicitly. `direct-gateway` and `prime-headless` SHALL remain
registered and selectable via `--executor`.

#### Scenario: Default executor is opencode

- **WHEN** `verdict orchestrate` is invoked with no `--executor` flag
- **THEN** coding nodes are dispatched to an OpenCode worker and the receipt
  records `harness: opencode`

#### Scenario: Explicit executor overrides the default

- **WHEN** `--executor direct-gateway` is passed
- **THEN** coding nodes use `direct-gateway` and `opencode` is not invoked

#### Scenario: Adapter registration rejects missing facets

- **WHEN** an adapter is registered with one or more `HarnessCapability` facets
  absent
- **THEN** registration raises, as required by ADR-041

### Requirement: Worker model calls go through verdict serve

All model calls from an OpenCode worker SHALL reach `verdict serve` (:8000).
Direct calls to OmniRoute (:20128) SHALL be impossible from within the worker.
Each request SHALL use exactly the route Verdict chose, and the receipt SHALL
attest that route identity.

#### Scenario: Worker model calls are relayed through serve

- **WHEN** an OpenCode worker makes a model call during a task
- **THEN** the call reaches `verdict serve` (:8000) and not OmniRoute (:20128)
  directly; the receipt shows the correct route

#### Scenario: Relay attests route identity

- **WHEN** an OpenCode worker's model call is forwarded by the relay
- **THEN** the attempt record in the receipt contains the route Verdict chose,
  not any route the worker could have substituted

#### Scenario: Worker is pinned to an admitted route other than serve's own pick

- **WHEN** a request carrying the `X-Verdict-Worker` header names, in `model`, a route
  that is in this request's admitted set but is not the route serve would choose
- **THEN** admission selects that route within the admitted set and mints its
  route-bound admission proof with the same minting function as for a chosen route;
  the provider follows the pinned route; there are no fallback routes; and the
  request is dispatched on that proof

#### Scenario: Worker pin to a route outside the admitted set is denied

- **WHEN** a request carrying the `X-Verdict-Worker` header names a route that is not
  in this request's admitted set (unknown, opaque such as `auto/*` or `combo/*`, or
  admitted in the catalog but without a fresh passport)
- **THEN** it is denied with 503, no proof is minted for any route, and upstream is
  never contacted; the admitted set is never widened

#### Scenario: Non-worker requests cannot pin a route

- **WHEN** a request without the `X-Verdict-Worker` header names a `model`, including
  a forged internal pin field in its JSON
- **THEN** routing is unchanged: the `model` field does not select the route

#### Scenario: Worker request without a valid model fails closed

- **WHEN** a request carrying the `X-Verdict-Worker` header has a missing, empty,
  null or non-string `model`
- **THEN** serve denies it without contacting upstream; it never falls through to
  automatic route selection

### Requirement: Per-attempt config isolation

Each OpenCode attempt SHALL write a fresh config to a new temp directory, set
`OPENCODE_CONFIG_DIR` (the temp directory) for that process only, and delete the temp directory on exit.
No session DB or config state SHALL be shared between attempts or concurrent
workers.

#### Scenario: Concurrent workers do not share session state

- **WHEN** two OpenCode workers run concurrently on separate nodes
- **THEN** their session DBs are isolated; neither attempt can read or write the
  other's session

#### Scenario: No credentials in temp config files or logs

- **WHEN** the executor writes the per-attempt config to the temp directory
- **THEN** the API key for `verdict serve` does not appear in the config file or
  in any log line

#### Scenario: Worker output is redacted before it is recorded

- **WHEN** the worker subprocess echoes the `verdict serve` key, or an
  `Authorization: Bearer` value, to stderr or into NDJSON error or text events
- **THEN** the executor replaces each such value with `[REDACTED]` before it
  reaches `WorkerTerminal`, events or logs, including a key that straddles the
  truncation window or is split across text events

### Requirement: Worktree-scoped edits; owned-files enforcement

An OpenCode worker SHALL edit only files inside its node worktree. After each
attempt, Verdict SHALL check all changed files against the owned-files list. Any
change outside owned files SHALL be classified as a failure without passing to
verification.

#### Scenario: Edit outside owned files is rejected

- **WHEN** an OpenCode worker edits a file outside its owned-files list
- **THEN** the attempt is classified as a failure; verification does not run

#### Scenario: Edit inside owned files passes to verification

- **WHEN** an OpenCode worker edits only files within the owned-files list
- **THEN** the owned-files check passes and the node's verification command runs

### Requirement: Attempt bounds apply to OpenCode workers

OpenCode worker attempts SHALL be bounded by the existing per-attempt timeout
(default and maximum 900 s), the idle-output stall kill
(`VERDICT_WORKER_IDLE_SECONDS`), and the attempts-per-node budget (default 4).

#### Scenario: Timeout kills the attempt

- **WHEN** an OpenCode attempt runs for 900 s without returning
- **THEN** the attempt is killed and classified as a timeout failure, not a hang

#### Scenario: Idle-output stall kills the attempt

- **WHEN** an OpenCode attempt produces no output for `VERDICT_WORKER_IDLE_SECONDS`
- **THEN** the attempt is killed and classified as an idle-kill failure

#### Scenario: Descendants are killed when the worker exits normally

- **WHEN** an OpenCode worker exits while a descendant with redirected pipes is
  still running in its process group
- **THEN** the executor kills the whole process group before returning, so no
  descendant can edit the worktree while owned-files validation runs

#### Scenario: Per-node budget is exhausted

- **WHEN** a node has used all its `max_attempts_per_node` attempts
- **THEN** the node ends FAIL_CLOSED, as required by the existing recovery budget
  invariant

### Requirement: OpenCode version pinned; doctor warns on mismatch

OpenCode SHALL be pinned at v2.3.2. `verdict doctor` SHALL warn (non-fatal)
when the installed OpenCode version differs from v2.3.2.

#### Scenario: Installed version matches pinned version

- **WHEN** OpenCode v2.3.2 is installed
- **THEN** `verdict doctor` does not show an OpenCode version warning

#### Scenario: Installed version differs from pinned version

- **WHEN** OpenCode at a version other than v2.3.2 is installed
- **THEN** `verdict doctor` shows a non-fatal warning naming the installed version
  and the pinned version
