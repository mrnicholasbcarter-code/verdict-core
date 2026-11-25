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

Superseded by the change `serve-worker-assignment` (2026-11-24): the `X-Verdict-Worker` header
no longer selects a route; the pin comes from a verified signed assignment.

- **WHEN** a request carrying a verified worker assignment names, in `model`, the signed route,
  and that route is in this request's admitted set but is not the route serve would choose
- **THEN** the assigned lane admits that route on every per-route gate and mints its
  route-bound admission proof with the same minting function as for a chosen route;
  the provider follows the pinned route; there are no fallback routes; and the
  request is dispatched on that proof

#### Scenario: A worker request carrying an execution-path request is rejected

Operator decision (2026-11-24): pinned execution-path support for workers is not built. The
execution-path flow selects its own offer, so a worker cannot be pinned through it; the
contract is rejection, not a pinned decision.

- **WHEN** a request with a verified worker assignment carries `execution_path_request`
  (present, whatever its value, on `/v1/chat/completions` or `/v1/responses`)
- **THEN** it is refused with 400 before any routing: nothing is routed, no proof is minted
  and upstream is never contacted; `IntelligenceService.route` independently refuses a
  worker-assigned route combined with `execution_path_request`

#### Scenario: A worker request never falls back to another route

- **WHEN** a worker request is accepted (its decision comes from the assigned lane admitting
  the signed route)
- **THEN** the relay bounds it to exactly the assigned route: it carries no
  alternatives and no fallback attempt is built, even if a decision were to list some
  (defence in depth)

#### Scenario: Worker pin to a route outside the admitted set is denied

- **WHEN** a request carrying a verified worker assignment signs a route that is not
  in this request's admitted set (unknown, opaque such as `auto/*` or `combo/*`, or
  admitted in the catalog but without a fresh passport)
- **THEN** it is denied with 503 naming the check that dropped the route, no proof is minted
  for any route, and upstream is never contacted; the admitted set is never widened

#### Scenario: Non-worker requests cannot pin a route

- **WHEN** a request without a verified worker assignment names a `model`, including
  a forged internal pin field in its JSON (the `X-Verdict-Worker` header alone is refused 403:
  superseded by `serve-worker-assignment`)
- **THEN** routing is unchanged: the `model` field does not select the route

#### Scenario: Worker request without a valid model fails closed

Superseded by `serve-worker-assignment` (2026-11-24): the `model` of a verified assignment must
equal the signed route exactly.

- **WHEN** a request carrying a verified worker assignment has a `model` that is missing, null,
  empty, not a string, or different from the signed route
- **THEN** serve refuses it with 400 before any routing and without contacting upstream; it
  never falls through to automatic route selection

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

### Requirement: The task is delivered once, as an instructions file in the attempt's config directory

The executor SHALL deliver the task text once: as an instructions file in the attempt's own
config directory (beside the per-attempt `opencode.json`, never in the worktree), listed in that
config's `instructions` together with the worktree's `AGENTS.md` when there is one.  The prompt
SHALL be one short, constant ask on stdin that names no task text, and `--title` SHALL be given so
OpenCode makes no second, title-agent request.  Nothing SHALL be written into the worktree and no
git command SHALL run for delivery (#1327).

#### Scenario: The task is an instructions file in the config directory

- **WHEN** an attempt launches
- **THEN** the instructions file exists in the config directory at launch and is listed in
  `instructions` with `AGENTS.md`; the temp directory, and the file with it, is removed on
  success, failure, timeout, stall, spawn failure and cancellation

#### Scenario: One short prompt carries no task text

- **WHEN** the worker is started
- **THEN** its stdin holds the one constant, bounded ask, no task text is in its arguments or
  its stdin, `--title` is given, the task text reaches OpenCode exactly once, and a task larger
  than one argument is delivered whole

#### Scenario: A missing or unusable instructions file fails the attempt before launch

- **WHEN** the instructions file is missing, empty, not a regular file or unreadable, or the task
  cannot be encoded
- **THEN** the attempt ends `config_write_failed:` before any worker is launched

#### Scenario: Nothing is written into the worktree

- **WHEN** an attempt runs, including in a repository that tracks a file named
  `.verdict-context.md`
- **THEN** the worktree is untouched (no `.verdict-context.md`), that tracked file is left as it
  is, and no git command runs

#### Scenario: The requests-library status forms are read

- **WHEN** a worker error reads `429 Client Error: Too Many Requests for url: ...` or
  `503 Server Error: Service Unavailable for url: ...`
- **THEN** the status (429, 503) is recorded and recovery reroutes; `retrying in 500 ms` and
  `failed after 429 attempts` give no status

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

#### Scenario: Pre-dispatch time counts against the attempt timeout

- **WHEN** the pre-dispatch checks (the binary probe, the serve readiness check and the assignment
  probe) take part of the attempt timeout, or one hangs
- **THEN** each check is bounded by what is left of the budget, the worker gets only the
  remaining budget, and when none remains the attempt ends as a timeout without spawning the
  worker (`test_pre_dispatch_time_counts_against_the_attempt_timeout`,
  `test_nothing_is_launched_when_the_pre_dispatch_checks_used_the_whole_budget`)

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

### Requirement: The worker's only credential is a signed per-attempt assignment; one pre-dispatch check

An OpenCode worker SHALL authenticate to `verdict serve` with one credential: a signed assignment
(`vwa1.<payload>.<tag>`) that the executor signs for that attempt, naming the route, the run id,
the node id, the time it was issued and the time it expires.  Serve's own token SHALL stay in the
orchestrator's process, for the readiness check alone, and SHALL NOT be placed in the worker's
environment, arguments, stdin or config files, or in anything recorded; it SHALL NOT be the
fallback when an assignment cannot be signed, and neither it nor the OmniRoute gateway key is ever
a worker credential.  The assignment SHALL reach the child only under the configured key variable
name, and it, its payload and its tag SHALL be redacted from everything recorded.  The run id is the
id of the run the node belongs to (the run directory's name, which the run receipt shows) and
the node id is the node's; both SHALL be carried from the runtime to the executor through
`run_executor` and `bounded_executor_call` as optional keywords, offered only to an executor that
accepts them, so every other executor is called exactly as before.

The assignment SHALL expire at its issue time plus the attempt timeout (rounded up to whole
seconds) plus 60 seconds.  An attempt whose lifetime would exceed the 1,800 seconds the format
allows SHALL fail closed before launch, and the lifetime SHALL NOT be shortened to fit.  A missing
or unreadable signing key, a missing run id or node id, a timeout that is not a positive finite
number, or an id the verifier would refuse SHALL each end the attempt with one
`harness_configuration:` message before anything is launched, with no request of any kind sent to
serve, and recovery SHALL treat it as a block.

A worker failure with HTTP status 401 or 403 and the message `authentication failed` (serve's
answer to a refused assignment) SHALL end the attempt as a `harness_configuration:` block, with no
cooldown for the route's provider, only when serve's receipts, read after the worker's process
group is gone, were read successfully, hold no request under the attempt's assignment, and are
shown to be the receipts of the serve the worker called.  Serve forwards a provider's own 401 or
403 unchanged, so the status and the words alone do not show that serve refused; when serve's
receipts hold a request under the assignment, or cannot be read, the failure SHALL stay the
provider's, exactly as the worker reported it.  The receipt database is found from the
orchestrator's configuration, not from the serve at `--serve-url`, so an empty read proves
nothing about that serve until the executor has found, in the same database and for the same
serve address, a request under one of its own earlier assignments (a digest only it issued, which
only that serve received).  Until then the failure SHALL stay the provider's and the attempt's
serve receipts SHALL be `unavailable` with a reason, never "zero requests".

Before an attempt writes or launches anything, the executor SHALL also check that the serve token
resolves, that the `opencode` binary runs and that serve is reachable and accepts the serve token;
a failure SHALL be one `harness_configuration:` message that recovery treats as a block.  Then,
before the receipts mark and the launch, it SHALL send serve one probe of the attempt's own
assignment: `POST /v1/chat/completions` with the assignment as the bearer (and not the serve
token) and a body that is only `{"model": <a value no route can equal>}`, with no `messages`,
followed by no redirect and bounded, as a whole exchange (a host-name lookup, a connection, an
answer sent slowly) and not only per socket operation, by what is left of the attempt's budget;
a request whose time has run out SHALL NOT be sent.  Serve verifies the
assignment and then refuses a `model` that differs from the assignment's route with 400, before
any routing, so the probe SHALL make no routing decision, mint no admission proof, send nothing
upstream and write no serve evidence.  A 400 with serve's body SHALL mean accepted and the attempt
SHALL go on.  A 403 with exactly serve's refusal body, or a 503 with exactly serve's `worker
assignments are not configured` body, SHALL end the attempt as one `harness_configuration:` block,
on a run's first attempt as on any other, with nothing launched, no upstream call and no provider
cooldown; its message SHALL name the causes the probe cannot tell apart (a different signing key
file or home, clocks more than 60 seconds apart, a serve older than the signed-assignment change
or running anonymous) and the fix, and SHALL NOT need serve's receipts.  Any other result (no
answer in time, a refused connection, a redirect, a 2xx, a 404, another 5xx, a 403 or 400 with
another body, an answer that ends before its declared length) SHALL be unproven: the attempt goes on exactly as before, and an unproven probe is
never a block and never a provider failure.  Serve's order (the model is refused before routing)
SHALL be pinned by a serve-side test that says the orchestrator relies on it.  The
checks SHALL be safe under cancellation: when the attempt is cancelled, the version probe's process
group SHALL be killed and reaped, no serve request SHALL be started afterwards, and the
cancellation SHALL complete only after the thread that ran the checks has finished.

#### Scenario: The worker holds only the signed assignment

- **WHEN** an attempt launches
- **THEN** the assignment appears in the child's environment under the key variable name only, and
  serve's token appears in none of the child's environment variables, arguments, stdin or config
  directory files; the assignment verifies with serve's verifier and names the attempt's route,
  run id and node id (`test_the_worker_holds_only_the_signed_assignment_and_never_the_serve_token`,
  `test_a_serve_token_in_the_process_environment_never_reaches_the_worker`)

#### Scenario: The readiness check carries the serve token only and the probe the assignment only

- **WHEN** the readiness check and the pre-launch probe run for an attempt
- **THEN** the readiness request (`GET /v1/guidance/status`) carries the serve token and never the
  assignment, and the probe (`POST /v1/chat/completions`, a relay path, the only kind that accepts
  an assignment) carries this attempt's own assignment and never the serve token; the assignment
  reaches serve nowhere else, and no redirect is followed with it
  (`test_serve_is_checked_with_the_serve_token_and_never_with_the_assignment`,
  `test_the_probe_carries_the_assignment_only_and_the_readiness_check_the_serve_token_only`,
  `test_a_redirect_is_never_followed_so_the_assignment_goes_nowhere_else`)

#### Scenario: A healthy serve accepts the probe and the probe costs nothing

- **WHEN** serve verifies the attempt's assignment and the probe's model differs from the
  assignment's route
- **THEN** serve answers 400 before any routing, so the probe makes no routing decision, mints no
  proof, sends nothing upstream and writes no evidence, serve logs one `model_mismatch` WARNING,
  the attempt goes on, and the probe's model equals no route a verified assignment can name
  (`test_a_healthy_serve_answers_the_probe_400_and_the_probe_decides_and_sends_nothing`,
  `test_the_orchestrators_assignment_probe_is_refused_before_routing_and_costs_nothing`,
  `test_the_probe_model_is_no_route_id_a_verified_assignment_can_carry`)

#### Scenario: Serve refuses the first assignment of a run, before launch

- **WHEN** serve answers the probe with its refusal (403 `authentication failed`) because the
  assignment is signed with a key serve does not hold, expired, or dated ahead of serve's clock, or
  with its 503 `worker assignments are not configured` because serve has no signing key
- **THEN** the attempt ends `harness_configuration:` as the run's first attempt: no worker is
  launched, nothing is sent upstream, no decision is made, no proof is minted, serve writes no
  evidence, recovery blocks with no cooldown, the message names the possible causes and
  `verdict setup`, and serve's receipts are not read, so a remote or spare serve is covered
  (`test_a_first_attempt_serve_refuses_is_a_configuration_block_before_anything_is_launched`,
  `test_a_serve_with_no_signing_key_is_the_same_block_naming_setup`,
  `test_a_serve_whose_receipts_the_orchestrator_cannot_read_is_covered_by_the_probe`,
  `test_serves_exact_refusal_ends_the_first_attempt_before_launch_naming_the_causes`,
  `test_serves_503_not_configured_ends_the_attempt_the_same_way_naming_setup`)

#### Scenario: An unclear probe result is unproven

- **WHEN** the probe gets no answer in time, a refused connection, a redirect, a 2xx, a 404, a 5xx
  other than serve's not-configured 503 (including "Proxy not initialized"), a 403 or 400 with a
  body other than serve's, a body that is too long or cut short (declared or chunked), or an answer
  sent so slowly that it outlasts the probe's time
- **THEN** nothing is concluded: the attempt goes on exactly as before, the worker is launched with
  its assignment, and it is never a block and never a failure of the attempt
  (`test_an_unclear_probe_result_is_unproven_and_the_attempt_goes_on_as_before`,
  `test_a_probe_that_gets_no_answer_in_time_is_unproven_and_the_attempt_goes_on`,
  `test_a_probe_to_nothing_listening_is_unproven`, `test_the_probe_cannot_raise_into_the_attempt`,
  `test_an_answer_that_trickles_in_is_cut_off_at_the_probes_deadline`,
  `test_an_answer_without_a_content_length_is_read_the_same`,
  `test_a_resolver_that_never_answers_cannot_hold_the_probe_past_its_deadline`,
  `test_a_probe_whose_connection_used_up_its_time_sends_nothing_more`)

#### Scenario: The probe counts against the attempt's budget

- **WHEN** the readiness check fails, uses up the budget, or the probe runs into the deadline, or
  the attempt is cancelled
- **THEN** a failed readiness check ends the attempt without a probe, the probe is given at most
  what is left of the attempt's budget, a refusal that arrives after the deadline is the attempt's
  timeout and not a block, and a cancelled attempt starts no probe and waits for one in flight
  (`test_a_failed_readiness_check_ends_the_attempt_with_no_probe`,
  `test_a_readiness_check_that_uses_up_the_budget_leaves_no_time_for_the_probe`,
  `test_the_probe_is_bounded_by_what_is_left_of_the_attempt`,
  `test_a_refusal_that_arrives_after_the_attempts_deadline_is_a_timeout_not_a_block`,
  `test_cancelling_run_waits_for_an_assignment_probe_that_is_already_in_flight`)

#### Scenario: The signer agrees with the independent signer

- **WHEN** the production signer and the independent test signer sign the same payload with the
  same key
- **THEN** the tokens are byte-identical, and serve's verifier accepts the production one
  (`test_a_minted_token_is_byte_identical_to_the_independent_signers`,
  `test_a_minted_token_is_accepted_by_the_verifier_with_the_signed_fields`)

#### Scenario: The lifetime follows the attempt timeout and is never clamped

- **WHEN** an attempt has a timeout of 100.2 s
- **THEN** its assignment lives 161 s; and a timeout whose lifetime would exceed 1,800 s ends the
  attempt `harness_configuration:` before launch with no key read and no request sent
  (`test_the_lifetime_is_the_attempt_timeout_rounded_up_plus_the_margin`,
  `test_a_timeout_that_would_need_more_than_1800_seconds_fails_closed_before_launch`,
  `test_a_lifetime_outside_one_to_1800_whole_seconds_is_refused_not_clamped`)

#### Scenario: Every live assignment has its own identity

- **WHEN** one executor signs two assignments for the same route, run and node within one clock
  second, sequentially or at once
- **THEN** the later one is dated a second later so that its digest differs, its lifetime is
  unchanged, and a request of the earlier attempt that serve commits afterwards is not counted for
  the later one; more than 31 live assignments for one route, run and node in one second end the
  attempt `harness_configuration:`
  (`test_each_live_assignment_of_one_executor_has_its_own_identity_even_in_one_second`,
  `test_concurrent_attempts_with_the_same_ids_get_distinct_identities`,
  `test_a_request_of_an_earlier_attempt_is_not_attributed_to_a_retry_in_the_same_second`,
  `test_an_expired_assignment_is_forgotten_so_the_clock_is_not_pushed_ahead_for_ever`,
  `test_more_live_assignments_than_the_identity_window_allows_is_a_block_not_a_collision`)

#### Scenario: A missing key or a missing id blocks before launch

- **WHEN** the signing key is missing, unreadable, too short or not a key, or the run id or the
  node id is missing or one the verifier would refuse
- **THEN** the attempt ends `harness_configuration:`, no worker is launched, nothing is written to
  the worktree, serve receives no request, the serve token is not used instead, and recovery
  blocks (`test_a_missing_or_unreadable_key_blocks_before_launch_and_never_falls_back`,
  `test_a_missing_run_id_or_node_id_blocks_before_launch`,
  `test_an_id_the_verifier_would_refuse_is_never_signed`)

#### Scenario: The run id and node id reach the executor from the runtime

- **WHEN** the runtime dispatches a node's attempt to an executor that accepts `run_id` and
  `node_id`
- **THEN** it receives the run directory's name and the node's id, through `MixedExecutor` and
  `FaultInjectingExecutor` too, and an executor that accepts neither is called exactly as before
  (`test_the_runtime_gives_the_executor_the_run_id_and_the_node_id`,
  `test_an_executor_without_the_id_keywords_is_called_exactly_as_before`,
  `test_mixed_executor_hands_the_ids_to_the_executor_it_picks`,
  `test_fault_injecting_executor_hands_the_ids_to_the_executor_it_wraps`)

#### Scenario: A planner call is never given an assignment

- **WHEN** a planner call is made with an `OpenCodeExecutor`
- **THEN** `AnswerOnlyExecutor` refuses it before any call, and an executor asked to run without a
  node id launches nothing
  (`test_a_planner_call_has_no_node_and_the_answer_only_wrapper_refuses_an_agent_executor`)

#### Scenario: A refusal after the probe passed fails closed with no upstream call

- **WHEN** the worker presents to the real serve an assignment that was altered after signing, or
  serve's key was replaced after the probe passed, as the first assignment of the run
- **THEN** serve refuses it with 403, nothing is sent upstream, serve writes no evidence, and the
  attempt fails; nothing yet shows that the database the executor read is the serve's it called,
  so the attempt is not called a configuration block: it stays the provider's denial and its
  serve receipts are `unavailable` with a reason
  (`test_a_tampered_assignment_fails_closed_with_no_upstream_call`,
  `test_a_key_rotated_after_the_probe_is_a_refusal_mid_run_and_stays_the_providers_at_first`)

#### Scenario: Serve's refusal after serve admitted an earlier assignment of the run is a block

- **WHEN** serve admitted an earlier assignment of this executor in its receipt database, and the
  worker then presents an assignment that was altered after signing, or serve's key was replaced
  after the probe passed
- **THEN** serve refuses it with 403, nothing is sent upstream, serve writes no evidence, and the
  attempt ends `harness_configuration:` (a block with no cooldown), not as the provider denying the
  account, because serve's receipts, shown to be the serve's it called, hold no request under the
  assignment (`test_a_refusal_after_serve_admitted_an_earlier_attempt_is_a_configuration_block`,
  `test_serves_refusal_of_the_assignment_is_a_configuration_block_not_a_provider_denial`,
  `test_a_401_with_serves_refusal_message_is_the_same_block`)

#### Scenario: A database not shown to be serve's does not decide who refused

- **WHEN** a worker fails with serve's refusal status and words and the receipt database the
  executor read is empty for the assignment but nothing has shown it is the one the serve at the
  worker's address writes (no request under an earlier assignment of the executor was found in
  it, the executor's earlier requests were found in a different database, or the file at the path
  is no longer the one they were found in: replaced, or a link retargeted, between attempts or
  during one)
- **THEN** the terminal keeps the worker's status and message, recovery sees the provider's denial
  (with a cooldown), and the terminal carries `serve_receipts` `unavailable` with the reason and
  `null` figures; what was shown for one database file or serve address does not vouch for another
  (`test_serves_words_are_the_providers_while_the_database_is_not_shown_to_be_serves`,
  `test_what_was_seen_in_one_database_does_not_vouch_for_another`,
  `test_what_was_seen_of_a_database_does_not_outlast_the_file`,
  `test_an_attempt_that_serve_never_served_does_not_show_the_database_to_be_serves`,
  `test_what_was_found_in_a_file_is_not_carried_to_the_file_that_replaces_it`,
  `test_what_was_found_for_one_serve_address_does_not_vouch_for_another`,
  `test_a_file_that_was_not_the_one_marked_is_not_vouched_for`,
  `test_a_file_replaced_while_it_is_read_is_unavailable`)

#### Scenario: Other 401 and 403 answers are still the provider's

- **WHEN** a worker fails with a 401 or 403 whose message is not serve's refusal, or serve's message
  with another status, even though serve's receipts, shown to be the serve's it called, hold no
  request under the assignment
- **THEN** recovery sees the status as before (`test_other_denials_are_still_read_as_the_providers`)

#### Scenario: Serve's words from a request serve admitted are the provider's

- **WHEN** a worker fails with a 401 or 403 whose message is serve's refusal text
  (`authentication failed`) and serve's receipts hold a request under the attempt's assignment,
  because serve admitted it and forwarded the upstream's own error unchanged
- **THEN** the attempt is not a configuration block: the terminal keeps the upstream's status and
  message, recovery classifies it as the provider's denial (with a cooldown), and the terminal
  carries `serve_receipts` `read` with `requests` 1
  (`test_an_admitted_request_whose_upstream_says_authentication_failed_is_the_providers`,
  `test_serves_words_after_a_request_serve_admitted_are_the_providers`)

#### Scenario: Serve's words stay the provider's when serve's receipts cannot be read

- **WHEN** a worker fails with serve's refusal status and words and serve's receipts are not
  configured, in memory, missing or unreadable
- **THEN** the origin of the error is not established, so the attempt is not blocked on the words
  alone: the terminal keeps the worker's status and message, recovery sees the provider's denial,
  and the terminal carries `serve_receipts` `unavailable` with its reason
  (`test_serves_words_are_the_providers_when_serves_receipts_cannot_be_read`)

#### Scenario: Unrunnable binary, unreachable serve or rejected serve token

- **WHEN** the binary does not run, serve is unreachable, or serve answers 401 or 403 to the
  readiness check
- **THEN** the attempt ends the same way, with a reason that never contains the serve token

#### Scenario: Cancelled during the version probe

- **WHEN** the attempt is cancelled while `opencode --version` is still running
- **THEN** the probe's process group is gone when the cancellation completes, and serve receives
  no request from that attempt, even if the probe would have finished later

### Requirement: The streaming waiver serve records is shown in the run receipt

`verdict serve` records, in its receipt database, the requirement it waived for a request served
under a verified assignment (today `streaming` only).  The executor SHALL read that back read-only
after the worker is gone, match it to its own assignment by digest (never by the credential) and
only among rows written after the worker started, and put it on the attempt's terminal; the
runtime SHALL record it on the `terminal` event, `receipt.json` SHALL carry it on that attempt, and
`verdict run-receipt` SHALL mark the attempt.  An attempt served without a waiver SHALL carry none,
an executor that signs nothing SHALL add nothing to its receipt, and a database that cannot be read
SHALL be reported as unavailable with its reason and null figures, never as "no waiver", and SHALL
NOT fail an attempt that worked; the same holds for a database that holds no request under the
assignment while nothing shows it is the database of the serve the worker called (the path comes
from the orchestrator's configuration, not from that serve), and for a file that is no longer the
one the mark was taken of (the mark's row watermark counts that file's rows, so it SHALL NOT be
applied to another).  The reader SHALL NOT create, migrate or write the database.

#### Scenario: An attempt served under the waiver carries it

- **WHEN** serve served three of an attempt's requests with `streaming` waived
- **THEN** the attempt's terminal, its `terminal` event and its row in `receipt.json` carry
  `requests: 3`, `requests_waived: 3` and the `streaming` waiver, and `verdict run-receipt` shows
  `streaming waived` on that attempt
  (`test_an_attempt_served_under_the_waiver_carries_it_on_its_terminal`,
  `test_the_signed_assignment_is_served_on_the_signed_route_and_the_waiver_is_read_back`,
  `test_an_attempt_served_under_the_waiver_carries_it_in_the_receipt`,
  `test_run_receipt_text_marks_the_attempt_that_ran_with_streaming_waived`)

#### Scenario: An attempt without the waiver carries none

- **WHEN** an attempt's requests needed no waiver, or its executor signs nothing
- **THEN** it carries an empty waiver list, or no assignment record at all, and its receipt is
  otherwise unchanged
  (`test_an_attempt_that_was_not_waived_carries_none`,
  `test_a_request_that_needed_no_waiver_leaves_the_attempt_without_one`,
  `test_an_attempt_that_was_not_served_under_the_waiver_does_not_carry_one`,
  `test_a_run_without_any_assignment_has_a_receipt_with_no_new_key`)

#### Scenario: Unreadable serve receipts are unavailable, not clean

- **WHEN** serve's receipt database is unconfigured, in memory, missing, damaged, or holds too many
  matching rows, or the read runs out of time, or it holds no request under the assignment and is
  not shown to be the database of the serve the worker called, or the file at the path was
  replaced (or a link retargeted) after the attempt's mark was taken, whatever rows it holds
- **THEN** the record is `unavailable` with a reason and `null` counts, the attempt's own result is
  unchanged, and nothing is created on disk
  (`test_unreadable_serve_receipts_are_unavailable_with_a_reason_and_the_attempt_still_ends_ok`,
  `test_a_missing_file_is_mark_zero_and_unavailable_at_read_time_and_is_never_created`,
  `test_too_many_matching_roots_is_unavailable_never_a_short_count`,
  `test_nothing_recorded_in_a_database_not_shown_to_be_serves_says_nothing_about_serve`,
  `test_nothing_recorded_is_zero_requests_once_the_database_is_shown_to_be_serves`,
  `test_a_request_under_the_assignment_is_read_with_or_without_a_shown_association`,
  `test_a_file_replaced_after_the_mark_is_unavailable_with_rows_either_side_of_its_watermark`,
  `test_a_file_replaced_after_the_mark_is_not_associated_with_the_serve_it_was_read_for`,
  `test_a_database_first_created_after_the_mark_is_read_from_its_first_row`,
  `test_an_unreadable_serve_receipt_is_null_with_a_reason_never_a_clean_attempt`)

#### Scenario: A waiver from before the attempt is not attributed to it

- **WHEN** serve's database already holds roots carrying the same digest from before the worker
  started
- **THEN** they are not counted (`test_a_request_of_an_earlier_attempt_is_not_attributed_to_a_retry_in_the_same_second`,
  `test_only_rows_after_the_mark_can_belong_to_the_attempt`)

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
