# Spec Delta

## ADDED Requirements

### Requirement: OpenCode adapter is registered as an opt-in worker executor

`DEFAULT_REGISTRY` SHALL register an `OpenCodeAdapter` as a worker executor for
`verdict orchestrate` that is selected only by name (`--executor opencode`). It SHALL NOT be
the default: `direct-gateway` SHALL remain the default worker executor until a later change
makes `opencode` the default after a live smoke passes (ADR-053 Decision 2, amended
2026-11-24). All nine `HarnessCapability` facets SHALL be declared explicitly. `direct-gateway`
and `prime-headless` SHALL remain registered and selectable via `--executor`.

#### Scenario: Default executor is unchanged

- **WHEN** `verdict orchestrate` is invoked with no `--executor` flag
- **THEN** coding nodes are dispatched to `direct-gateway`, `opencode` is not
  invoked, and the receipt does not record `harness: opencode`

#### Scenario: OpenCode is selected by name

- **WHEN** `--executor opencode` is passed, or a node is mapped to it with
  `--executor-map node=opencode`
- **THEN** those coding nodes are dispatched to an OpenCode worker and the receipt
  records `harness: opencode`

#### Scenario: Explicit executor overrides the default

- **WHEN** `--executor prime-headless` is passed
- **THEN** coding nodes use `prime-headless` and neither `direct-gateway` nor `opencode`
  is invoked

#### Scenario: The help text says OpenCode is opt-in

- **WHEN** `verdict orchestrate --help` is shown
- **THEN** the `--executor` option names `direct-gateway` as the default and says OpenCode
  is selected with `--executor opencode`

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

#### Scenario: A worker request carrying an execution-path request is rejected

Operator decision (2026-11-24): pinned execution-path support for workers is not built. The
execution-path flow selects its own offer, so a worker cannot be pinned through it; the
contract is rejection, not a pinned decision.

- **WHEN** a request with the `X-Verdict-Worker` header carries `execution_path_request`
  (present, whatever its value, on `/v1/chat/completions` or `/v1/responses`)
- **THEN** it is refused with 400 before any routing: nothing is routed, no proof is minted
  and upstream is never contacted; `IntelligenceService.route` independently refuses a
  worker-assigned route combined with `execution_path_request`

#### Scenario: A worker request never falls back to another route

- **WHEN** a worker request is accepted (its decision comes from admission pinning the
  assigned route)
- **THEN** the relay bounds it to exactly the assigned route: it carries no
  alternatives and no fallback attempt is built, even if a decision were to list some
  (defence in depth)

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

#### Scenario: Output flood kills the attempt

- **WHEN** an OpenCode attempt prints more than the output cap (16 MiB of stdout plus
  stderr by default) without ending
- **THEN** the process group is killed and the attempt is classified as an
  `output_limit` failure; the orchestrator never buffers unbounded worker output

#### Scenario: Context preparation counts against the attempt timeout

- **WHEN** preparing the context file (including its git tracking probes) takes part of
  the attempt timeout, or a probe hangs
- **THEN** each probe is bounded by what is left of the budget, the worker gets only
  the remaining budget, and when none remains the attempt ends as a timeout without
  spawning the worker

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

- **WHEN** OpenCode at a version other than v2.3.2 is installed, including a
  prerelease or build of the same numbers such as `2.3.2-rc.1`
- **THEN** `verdict doctor` shows a non-fatal warning naming the installed version
  and the pinned version

### Requirement: Worker credential resolved by Verdict; one pre-dispatch check

An OpenCode worker SHALL authenticate to `verdict serve` with the serve bearer token that
Verdict resolves itself (environment, credential store, `serve.env`), never the OmniRoute gateway
key and never an undocumented operator variable.  It SHALL reach the child only under the
configured key variable name and SHALL be redacted from everything recorded.  Before an attempt
writes or launches anything, the executor SHALL check that the credential resolves, that the
`opencode` binary runs and that serve is reachable and accepts the credential; a failure SHALL be
one `harness_configuration:` message that recovery treats as a block.  The checks SHALL be safe
under cancellation: when the attempt is cancelled, the version probe's process group SHALL be
killed and reaped, no serve request SHALL be started afterwards, and the cancellation SHALL
complete only after the thread that ran the checks has finished.

#### Scenario: Credential reaches only the child environment

- **WHEN** an attempt launches
- **THEN** the credential appears in the child's environment under the key variable name only,
  not in its arguments, its config file or any other variable

#### Scenario: Missing credential fails once, before launch

- **WHEN** no serve credential can be resolved
- **THEN** the attempt ends with a `harness_configuration:` terminal, nothing is written to the
  worktree, no worker is launched and recovery blocks instead of retrying

#### Scenario: Unrunnable binary, unreachable serve or rejected credential

- **WHEN** the binary does not run, serve is unreachable, or serve answers 401 or 403
- **THEN** the attempt ends the same way, with a reason that never contains the credential

#### Scenario: Cancelled during the version probe

- **WHEN** the attempt is cancelled while `opencode --version` is still running
- **THEN** the probe's process group is gone when the cancellation completes, and serve receives
  no request from that attempt, even if the probe would have finished later

### Requirement: Serve address comes from configuration

Workers SHALL call the serve address given by `--serve-url`, else `VERDICT_SERVE_URL`, else the
serve unit's default `http://127.0.0.1:8000`.  An address that is not an http(s) origin SHALL be
refused, not replaced by the default.

#### Scenario: Configured address is used

- **WHEN** `--serve-url http://127.0.0.1:9123` is passed
- **THEN** the per-attempt provider base URL is `http://127.0.0.1:9123/v1`

### Requirement: The dispatch-origin check binds the executor to the worker serve address

The OpenCode executor SHALL report, as its dispatch origin, the `verdict serve` origin its
workers really call (the address resolved as in "Serve address comes from configuration"), and
SHALL NOT declare that it sends nothing. The run's dispatch authority SHALL carry the worker
serve origin, which `verdict orchestrate` resolves from the same flag and variable, and the
runtime SHALL refuse the send, before any permit is minted and before a worker is launched, when
the reported serve origin differs from it (both normalised) or when the authority carries none.
A reported serve origin SHALL be checked against the worker serve origin only, never against the
gateway origin. An origin reported by any other executor SHALL be checked against the gateway
origin only, as before, and SHALL NOT be accepted for naming the serve address. Executors that
send nothing SHALL be unchanged. The check SHALL hold when the executor is wrapped for a node map
or for fault injection.

#### Scenario: The reported origin is the one the worker calls

- **WHEN** the executor is built with `--serve-url http://127.0.0.1:9123`
- **THEN** it reports `http://127.0.0.1:9123`, and the per-attempt provider base URL is
  `http://127.0.0.1:9123/v1`

#### Scenario: The bound serve address is sent

- **WHEN** the dispatch authority binds `http://127.0.0.1:8000` and the executor reports it, in
  any spelling that normalises to it
- **THEN** the permit is minted and the worker is launched

#### Scenario: Another serve address is refused before dispatch

- **WHEN** the executor reports `http://127.0.0.1:8000` and the authority binds
  `http://127.0.0.1:9999`
- **THEN** the node is BLOCKED with `dispatch_denied: executor origin mismatch`, no permit is
  minted and no worker is launched, even when the authority's gateway origin is
  `http://127.0.0.1:8000`

#### Scenario: No serve address bound is refused

- **WHEN** the authority carries no worker serve origin (the configured address is not an
  http(s) origin)
- **THEN** the node is BLOCKED with `dispatch_denied: executor origin mismatch: no worker serve
  origin is bound`, with no permit and no launch

#### Scenario: Another executor cannot claim the serve address

- **WHEN** an executor other than the OpenCode one reports a plain URL equal to the worker serve
  origin and the authority's gateway origin is another
- **THEN** the node is BLOCKED with `dispatch_denied: executor origin mismatch`

#### Scenario: Executors that send nothing are unchanged

- **WHEN** an executor declares the local origin, whatever worker serve origin the authority
  carries or none
- **THEN** the permit is minted and the executor runs as before

#### Scenario: Wrapped executors are checked the same

- **WHEN** the OpenCode executor runs inside a node map or the fault-injecting wrapper
- **THEN** a matching serve origin is sent and a different one, or none, is refused before dispatch

### Requirement: Upstream status and cost are recorded honestly

A failed attempt SHALL keep the HTTP status and retry delay OpenCode reports in a structured
`error` event.  When that event carries a message but no status, the HTTP status printed on
stderr (sanitized, secrets redacted, read before any truncation) SHALL be used as the status,
while the event's message stays the recorded error; a status the event does carry SHALL NOT be
replaced by one from stderr.  A status read from unstructured text (stderr, or an error message
with no `status` field) SHALL need an HTTP or status marker (`HTTP 429`, `HTTP/1.1 503`,
`status 503`, `status code: 401`) or its standard reason phrase (`429 Too Many Requests`): a bare
4xx or 5xx number, such as a retry delay or an attempt count, SHALL NOT be a status.  `cost_usd`
SHALL be recorded only for a real positive figure; a reported 0, a
missing figure, or any unpriced step SHALL give `null` with a reason, and tokens SHALL still be
recorded.

#### Scenario: Upstream 429 keeps its status

- **WHEN** OpenCode exits 1 after an error event with `status` 429 and `retryAfterMs` 7000
- **THEN** the terminal carries status 429 and a 7 s retry delay

#### Scenario: Stderr supplies the status when the error event has none

- **WHEN** OpenCode exits 1 after an error event with the message `request failed` and no
  status, and prints `HTTP 429 Too Many Requests` on stderr
- **THEN** the terminal carries status 429 and the error `request failed`

#### Scenario: An incidental number is not a status

- **WHEN** OpenCode exits 1 and its only error text is `retrying in 500 ms` or
  `failed after 429 attempts`
- **THEN** the terminal carries no status, so recovery does not reroute it as a rate limit or an
  outage

#### Scenario: A marked status is found anywhere in the text

- **WHEN** OpenCode exits 1 and its error text ends with `HTTP 503 Service Unavailable` or
  `status code: 401`, after a long diagnostic that holds an incidental number
- **THEN** the terminal carries status 503 or 401

#### Scenario: Zero cost is not recorded

- **WHEN** OpenCode reports a cost of 0 for a step
- **THEN** `cost_usd` is `null` with a reason, and the token counts are recorded
