# Tasks

## 1. Licence and terms

- [x] 1.1 Read the OpenCode v2.3.2 licence and terms of service from its
      repository and site. Record the SPDX licence identifier, the terms-of-service
      URL, any usage restrictions relevant to automated agentic use, and the date
      checked in the "Licence and terms" section of ADR-053. Verify: the section in
      ADR-053 is populated and no placeholder text remains.

## 2. Adapter and executor

- [x] 2.1 Add `OpenCodeAdapter` to `verdict/orchestration/harness.py` with all
      nine `HarnessCapability` facets declared explicitly (ADR-041). Register it in
      `DEFAULT_REGISTRY` as an opt-in executor (selected with `--executor opencode`; not
      `default=True`; ADR-053 Decision 2 amended 2026-11-24), keeping `direct-gateway` as the
      default and `prime-headless` registered. Add `harness-opencode` capability label to the
      registry executor map. Verify: `DEFAULT_REGISTRY.names()` lists `opencode`,
      `direct-gateway` and `prime-headless`, and the default stays `direct-gateway`
      (`tests/test_harness_registry.py`, task 2.9).
- [x] 2.2 Write `OpenCodeExecutor` in `verdict/orchestration/executors.py` (or a
      new `verdict/orchestration/harness_opencode_executor.py`). The executor
      writes a per-attempt config to a temp directory, sets `OPENCODE_CONFIG_DIR`,
      launches `opencode run --auto --standalone --format json
      --model verdict/<route_id> --session <attempt_id> --archived`,
      enforces the 900 s timeout ceiling, kills on idle-output stall
      (`VERDICT_WORKER_IDLE_SECONDS`), collects JSON output, maps it to
      `WorkerTerminal`, and deletes the temp directory on exit. Verify: unit tests
      for the executor, including timeout kill, idle kill, non-zero exit, and
      malformed JSON.
- [x] 2.3 Per-attempt config isolation: each attempt writes a fresh config to a
      new temp directory. The config sets the provider base URL to
      `http://127.0.0.1:8000/v1`, maps `verdict/<route_id>` to `<route_id>`, sets
      `permissions` to restrict file edits to the node worktree, and denies
      `external_directory`. The API key is not written to the config file or to
      any log. Verify (tests, `tests/test_opencode_executor.py`): the config holds no key
      (`test_write_attempt_config_no_plaintext_key`, `test_opencode_executor_config_has_no_plaintext_key`)
      and the key is redacted everywhere it could be recorded
      (`test_executor_redacts_worker_key_everywhere`); each attempt's config, data and state
      directories are its own and are removed afterwards, including when two attempts run at
      the same time (`test_opencode_executor_temp_dirs_cleaned_up`,
      `test_two_concurrent_attempts_never_share_config_data_or_state`) and when the temp
      filesystem fails (`test_a_full_temp_filesystem_is_a_failed_terminal_not_an_exception`).
      The live run with the real OpenCode binary is task 2.8; it is not ticked here.
- [x] 2.5 (serve side superseded 2026-11-24 by `serve-worker-assignment`; the worker's credential
      is superseded by the signed assignment of tasks 2.14 to 2.16, #1325) Pre-dispatch check:
      resolve serve's bearer token with Verdict's own code (now used in the orchestrator only, for
      the readiness check), and check once before launch that it resolves, the binary runs and
      serve accepts it (one `harness_configuration:` message, treated as a block). Verify: tests
      in `tests/test_opencode_executor.py` and `tests/test_serve_service_resolvers.py`.
- [x] 2.14 Production signer (#1325): `mint_assignment` and `assignment_digest` in
      `verdict/worker_assignment.py`, next to the verifier, same wire format, key from
      `load_assignment_key`; it sets `iat` from the clock, refuses (never clamps) a lifetime over
      1,800 s, an id, key, clock or token length the verifier would refuse, and gives the same
      token as the independent signer in `tests/worker_assignment_helpers.py`. Verify:
      `tests/test_worker_assignment.py` (`test_a_minted_token_is_byte_identical_to_the_independent_signers`,
      `test_a_minted_token_is_accepted_by_the_verifier_with_the_signed_fields`,
      `test_a_lifetime_outside_one_to_1800_whole_seconds_is_refused_not_clamped`,
      `test_an_id_the_verifier_would_refuse_is_never_signed`,
      `test_a_token_the_verifier_would_refuse_for_its_length_is_never_returned`).
      **Evidence (2026-11-25):** the 66 new tests failed before `mint_assignment` existed
      (`AttributeError`) and all 218 tests of the file pass after; changing the signer to write
      non-ASCII unescaped, or to accept a lifetime of 0 or 1,805 s, fails the parity and lifetime tests.
- [x] 2.15 Carry the run id and the node id (#1325): `run_executor` and `bounded_executor_call`
      take optional `run_id` and `node_id` and pass each only to an executor that accepts the
      keyword; the runtime passes the run directory's name and the node id; `MixedExecutor` and
      `FaultInjectingExecutor` forward them; planner calls pass none. Verify:
      `tests/test_worker_identity_carry.py`, `tests/test_orch_runtime.py`
      (`test_the_runtime_gives_the_executor_the_run_id_and_the_node_id`,
      `test_an_executor_without_the_id_keywords_is_called_exactly_as_before`).
      **Evidence (2026-11-25):** 9 of the 11 tests of `tests/test_worker_identity_carry.py` and the
      runtime test failed before the change (`TypeError: ... unexpected keyword argument 'run_id'`);
      the two that passed are the "called exactly as before" guards.
- [x] 2.16 The executor signs one assignment per attempt and hands the worker nothing else (#1325):
      lifetime = the attempt timeout rounded up + 60 s, refused before launch above 1,800 s; a
      missing or unreadable key, a missing run id or node id or a bad timeout is a
      `harness_configuration:` block before launch, with no request to serve and no fallback to the
      serve token; the serve token is used only for the readiness check; serve's own 403
      `authentication failed` is a configuration block, not a provider denial, but only when
      serve's receipts (read after the worker is gone) hold no request under the assignment and
      are shown to be the receipts of the serve the worker called (an earlier assignment of the
      same executor was found in the same database); the same words from a request serve
      admitted, with receipts that cannot be read, or with a database not shown to be serve's, stay
      the provider's (a provider's 401 or 403 is forwarded by serve unchanged); the
      `X-Verdict-Worker` header is no longer sent. Verify: `tests/test_opencode_signed_assignment.py`,
      `tests/test_opencode_executor.py`
      (`test_the_worker_holds_only_the_signed_assignment_and_never_the_serve_token`,
      `test_the_assignment_never_reaches_terminal_events_or_logs`,
      `test_write_attempt_config_sends_no_worker_header_and_reads_the_key_from_the_environment`).
      **Evidence (2026-11-25):** each of these changes was reverted in turn and the tests failed:
      the serve token to the child (11 failures), no 1,800 s refusal (5), clamping (5), the
      assignment to the readiness check (1), no refusal mapping (2), margin 0 (8), no launched flag
      (8), a stale mark (3), no run id check (5), no timeout check (3), the serve token as the
      fallback (34). After review round 1 (2026-11-25) each live assignment of an executor also has
      its own digest, so serve's receipts cannot be attributed to the wrong attempt of one node
      in one second (the identity tests fail with that logic removed). After the pull request
      review (2026-11-25) the refusal is decided from serve's receipts, not from the words alone:
      the 8 new tests (`test_an_admitted_request_whose_upstream_says_authentication_failed_is_the_providers`
      with 401 and 403, `test_serves_words_after_a_request_serve_admitted_are_the_providers`
      with 401 and 403, and `test_serves_words_are_the_providers_when_serves_receipts_cannot_be_read`
      with four kinds of unreadable receipts) failed with the gate removed (the old behaviour:
      a block on the words alone), and the refusal tests failed with a gate that never blocks
      (6 failures). After the second pull request review (2026-11-25, round 4) an empty read
      counts only for a database shown to be the called serve's: the receipt database is found
      from the orchestrator's configuration, not from the serve at `--serve-url`, so with a spare
      or remote serve zero rows in it did not show that serve refused and could suppress a
      provider's cooldown. Written first against the unchanged code, 10 tests failed
      (`tests/test_serve_evidence.py` 3, `tests/test_opencode_signed_assignment.py` 3 and
      `tests/test_opencode_assignment_serve_path.py` 4: the first-attempt refusals were blocks);
      with the association never recorded 13 failed (every block test), and with any database seen
      vouching for every database `test_what_was_seen_in_one_database_does_not_vouch_for_another`
      failed. The accepted cost is that a signing key serve does not share, refused from a run's
      first attempt, is not recognised as a block.
- [x] 2.17 The executor-to-serve path through the real app (#1325): a loopback server runs the real
      `verdict serve` app with a recording upstream; the real executor signs and a fake worker sends
      one request; the assignment is served on the signed route and serve's evidence names the
      run, the node and the route; an expired, tampered, foreign-key or future-dated assignment
      gets 403 with no upstream call and no evidence, as the run's first assignment (the attempt
      fails, its serve receipts `unavailable`, not a block) and after serve admitted an earlier
      one (a configuration block). Verify: `tests/test_opencode_assignment_serve_path.py`.
- [x] 2.18 The streaming waiver serve records is shown in the run receipt (#1325): serve's receipt
      database is read back read-only by digest (`verdict/orchestration/serve_evidence.py`,
      `serve_service.resolve_serve_receipts_db`), the terminal carries `worker_assignment`, the
      runtime records it on the `terminal` event, `receipt.json` projects it and `verdict
      run-receipt` marks the attempt. Verify: `tests/test_serve_evidence.py`,
      `tests/test_serve_service_resolvers.py`, `tests/test_run_receipt_worker_assignment.py`,
      `tests/test_opencode_signed_assignment.py` (the read-back section).
      **Evidence (2026-11-25):** 6 of the 14 tests of `tests/test_run_receipt_worker_assignment.py`
      and 6 resolver tests failed before the change; mutations of the reader (no row mark, roots
      only, no JSON guard, read-write open, no row limit, no deadline) each fail a test.
- [x] 2.19 Live smoke of the signed assignment (#1325, operator consent in the issue, one
      attempt, 2026-11-25): a spare `verdict serve` from this branch on port 18325 with a private
      copy of the state in a temp directory and its own signing key, never port 8000 and never the
      real `~/.verdict`; one `OpenCodeExecutor` attempt through the real gateway on
      `cc/claude-haiku-5-5` (an admitted, already-paid subscription route), in a scratch git
      repository. Verify: the edit lands, the terminal is ok, and serve's receipts show the
      pinned route. **Evidence:** run at head `538cf004`: terminal `ok: true`, `stop_reason`
      `stop`, 14.0 s, 250 input and 411 output tokens, `cost_usd` null with the reason `not_reported`;
      `greet.py` now returns `"hello, " + name` and the verification command passed; serve served
      three `POST /v1/chat/completions` (HTTP 200) and wrote three evidence roots, each
      `selected`, `model` and `resolved_route` `cc/claude-haiku-5-5`, `worker_assignment`
      `{route cc/claude-haiku-5-5, run run-oc1c-smoke, node smoke-1}` with the same digest,
      `requirement_waivers` `streaming` on all three, and the executor's terminal read the same back
      (`requests: 3`, `requests_waived: 3`). No second attempt was made. The real `~/.verdict` and
      `~/.config/verdict` were not written. Raw output is in the pull request.
      **Gateway traffic, read back afterwards from OmniRoute's call log (a read-only GET on its
      management API) for `claude-haiku-5-5` between 11:31:14Z and 11:31:28Z:** four POSTs, not
      three. The first, at 11:31:20.97Z, is 40 input and 1 output token (444 ms); the other three, at
      11:31:23Z, 11:31:25Z and 11:31:28Z, are OpenCode's own (about 8.4 thousand input tokens each,
      most of them cache reads). The 1-token request was sent by nothing this story wrote; it is
      consistent with serve's own budgeted confirm of the pinned route (the route's stored passport
      was older than the 10-minute permit-proof window, and the dry run of 11:29, which had no live
      confirm transport, denied the same route `passport_stale`), but the temp state that would
      show it was removed, so this is read from the gateway log alone and the cause is not
      established. Before the attempt, the dry runs called the gateway's management API (reads,
      no model call) and two loopback rehearsals ran the real OpenCode binary against a local
      stand-in for serve (no gateway, no model).
- [ ] (post-merge) 2.20 The same one-attempt smoke on installed main, after `verdict setup --apply
      --allow host.serve` has created the signing key and restarted serve: an `OpenCodeExecutor`
      attempt on an admitted route lands an edit in a scratch repository and the installed serve's
      receipt shows the pinned route. Record the raw output in the story's DoD evidence file.
- [x] 2.6 Serve address from configuration (`--serve-url`, `VERDICT_SERVE_URL`, default
      `http://127.0.0.1:8000`). Verify: `tests/test_orch_executors.py`,
      `tests/test_serve_service_resolvers.py`.
- [x] 2.7 Keep the upstream HTTP status of a structured error; never record a cost of 0.
      Verify: `tests/test_opencode_executor.py`.
- [x] 2.4 (the header pin is superseded 2026-11-24 by the signed assignment of
      `serve-worker-assignment`) Route enforcement: verify that every model call from an OpenCode worker
      reaches `verdict serve` (:8000). If per-request route pinning is absent from
      the relay, add it so that each request uses exactly the route Verdict chose
      and the receipt attests that model identity. Verify: a test or live check
      confirms no call reaches OmniRoute (:20128) directly; the receipt shows the
      correct route.
- [x] 2.9 `direct-gateway` stays the default worker executor; OpenCode is opt-in with
      `--executor opencode` (operator decision, 2026-11-24; ADR-053 Decision 2 amended). The
      `--executor` help says so. Verify: `tests/test_harness_registry.py`
      (`test_registry_resolves_production_adapters_and_aliases`,
      `test_opencode_is_registered_and_selectable_but_not_the_default`) and
      `tests/test_orch_executors.py` (`test_cli_parser_exposes_planner_executor_role`,
      `test_default_executor_builds_direct_gateway_not_prime`,
      `test_opencode_executor_is_built_only_when_named`,
      `test_orchestrate_help_says_opencode_is_opt_in`); each fails against a registry that makes
      `opencode` the default.
- [x] 2.10 The HTTP status printed on stderr is the fallback when an NDJSON error carries a
      message but no status; stderr is sanitized in every case and the NDJSON message stays the
      recorded error (the last open review finding of the first review rounds). Verify:
      `tests/test_opencode_executor.py`
      (`test_stderr_status_is_the_fallback_when_the_ndjson_error_has_none`,
      `test_a_status_in_a_long_stderr_is_found_when_the_ndjson_error_has_none`,
      `test_a_structured_status_beats_stderr_when_both_exist`,
      `test_a_secret_in_stderr_is_redacted_before_a_status_is_read_from_it`).
- [x] 2.11 An HTTP status is read from unstructured text (stderr, an error message with no
      `status` field) only when it carries an HTTP or status marker or its standard reason
      phrase; a delay or a count (`retrying in 500 ms`, `failed after 429 attempts`) is not a
      status, and a marked status is still found at the end of a long text. Explicit structured
      statuses are unchanged (review of 384b5c82). Verify: `tests/test_opencode_executor.py`
      (`test_a_number_without_an_http_or_status_marker_is_not_a_status`,
      `test_a_marked_http_status_in_unstructured_text_is_still_found`,
      `test_a_marked_status_at_the_end_of_a_long_text_is_still_found`,
      `test_a_marked_status_pushed_out_of_the_tail_is_still_found_past_an_incidental_number`,
      `test_an_incidental_number_in_a_failed_attempt_gives_no_status`,
      `test_an_incidental_number_in_the_ndjson_message_does_not_hide_a_marked_stderr_status`).
- [x] 2.12 The pre-dispatch checks are safe under cancellation: the version probe is killed and
      reaped, no serve request starts after the cancel, and the cancellation completes only
      after the checks' thread has finished (review of 384b5c82). Verify:
      `tests/test_opencode_executor.py`
      (`test_cancelling_run_kills_the_version_probe_and_waits_for_it`,
      `test_cancelling_run_during_the_version_probe_makes_no_serve_request`,
      `test_cancelling_run_between_the_binary_and_serve_checks_skips_the_serve_check`,
      `test_cancelling_run_waits_for_a_serve_check_that_is_already_in_flight`).
- [x] 2.13 Dispatch-origin check on for the OpenCode executor (#1326, story 3 of #1313): the
      executor reports the serve origin it really calls instead of declaring
      `LOCAL_EXECUTOR_ORIGIN`, `DispatchAuthority.worker_serve_origin` carries the address
      `verdict orchestrate` resolves, and the runtime refuses a mismatch, or no bound address,
      before the permit and the launch; other executors are not loosened. The stopgap
      declaration and its `DISPATCH_ORIGIN_CONFLICT` note are removed from
      `executor_opencode.py`. Verify (tests):
      `tests/test_opencode_executor.py`
      (`test_the_executor_reports_its_real_serve_origin_not_the_local_declaration`,
      `test_the_reported_origin_is_the_one_the_worker_calls`,
      `test_the_local_origin_declaration_and_its_stopgap_note_are_gone`) and
      `tests/test_dagruntime_dispatch_permits.py`
      (`test_opencode_on_the_bound_worker_serve_origin_is_sent`,
      `test_opencode_origin_is_compared_normalised`,
      `test_opencode_on_another_serve_than_the_authority_binds_is_refused`,
      `test_opencode_with_no_worker_serve_origin_bound_is_refused`,
      `test_a_serve_origin_is_never_checked_against_the_gateway_origin`,
      `test_another_executor_cannot_claim_the_worker_serve_origin`,
      `test_another_executor_on_the_gateway_origin_is_unchanged_by_a_serve_origin`,
      `test_an_executor_that_sends_nothing_is_allowed_whatever_serve_origin_is_bound`,
      `test_the_serve_origin_check_holds_through_the_executor_wrappers`,
      `test_dispatch_origin_problem_table`,
      `test_orchestrate_authority_carries_the_worker_serve_origin`,
      `test_the_authority_and_the_opencode_executor_built_from_the_same_arguments_agree`).
      **Evidence (2026-11-24):** the tests were written first and 31 of them failed on the
      unchanged code; with the change all 238 tests of `tests/test_opencode_executor.py`,
      `tests/test_dagruntime_dispatch_permits.py` and `tests/test_dispatch_permits.py` pass. The live
      path through serve is tasks 2.8 and 8.1; serve's own proof that it forwards to the permit's
      gateway and the signed assignment stay with #1313.
- [ ] (post-merge) 2.8 Live smoke of the worker on an admitted route, run with
      `--executor opencode` (OpenCode is opt-in; it failed until #1313 landed, because serve
      refused the worker's requests before it honoured the worker's assignment; the serve side
      merged in #1335 and the executor signs the assignment in #1325, whose branch smoke is task
      2.19; this task is the run through `verdict orchestrate` on installed main): one attempt through
      `OpenCodeExecutor`, in a scratch git repository, against a spare `verdict serve` with a
      private copy of the state, pinned to the cheapest admitted SUBSCRIPTION route (the
      operator's decision of 2026-11-24). Verify: the edit lands in the scratch repository, the
      attempt's exit code is 0, tokens are recorded, serve's receipt shows the pinned route, the
      raw `step_finish.cost` for that route is recorded (does OpenCode report 0 for a custom
      provider?), OpenCode's session DB and state land under the per-attempt directories, and no
      credential appears in the live config or in any log.
      **Run once, not completed (2026-11-24):** the spare serve admitted three subscription routes
      when probed (`antigravity/gemini-3.1-flash-lite`, `cc/claude-haiku-5-5`,
      `cc/claude-opus-5-5`); the attempt was pinned to `cc/claude-haiku-5-5`, the lowest list
      price in Verdict's model metadata ($0.10 in and $0.50 out per million tokens, against $0.25
      and $1.50, and $4.00 and $20.00). The pre-dispatch checks passed and OpenCode ran (exit 1,
      10 retries), but serve refused all 13 of the worker's own requests before any upstream
      call: 11 were decided `fallback` ("critical — never offload", escalation `money-path`) and
      2 were denied ("fail_closed — empty active ∩ fresh-passport ∩ confirmed intersection",
      escalation `auth-security`); the receipts of those two list the requirements `streaming`
      and `tools` and drop every one of 3074 candidates as `required_unknown` ("Core metadata
      store unavailable"). So no edit landed, no tokens were reported, and no `step_finish` event
      (the only carrier of `cost`) was produced. The per-attempt config held no key and its
      directory was removed; no request was sent upstream. This is how serve admits a worker's
      requests, not the adapter or a missing passport; it was not worked around (no admission
      rule or passport was changed). The operator decided on 2026-11-24 to merge the adapter as
      opt-in and to move the serve fix to #1313 (serve honours a worker's assignment before its
      own escalation and tier classification; the capability gate gets metadata on the relay
      path); at the time this task and 8.1 were expected to fail until #1313 landed (it has since:
      serve's side in #1335 and the executor's in #1325, whose branch smoke is 2.19), and OpenCode
      becomes the default only after this smoke passes on installed main. An earlier attempt on a free route
      (`run-20261124T042116Z`) was also denied, for other admission reasons (`passport_stale`,
      `confirm_budget_exhausted`). Raw output:
      `/tmp/claude-takeover/oc1-smoke/run-20261124T051739Z/` (`smoke-run-transcript.txt`,
      `worker-denial-evidence.json`).

## 3. Post-attempt enforcement

- [x] 3.1 After each attempt, Verdict computes the diff and checks all changed
      files against the owned-files list. A diff outside owned files is rejected
      and classified as a failure. Verify: a test with a file edit outside owned
      files is classified as a failure, not passed to verification.
- [x] 3.2 After a successful owned-files check, run the node's verification
      command and the repo gates (ruff, mypy, tests). OpenCode's own checks are
      advisory and do not replace Verdict's gates. Verify: a node whose
      verification command fails is classified as a failure.

## 4. Task delivery

- [x] 4.1 Deliver the task as an instructions file in the per-attempt config directory and one
      short prompt on stdin with `--title`; remove `.verdict-context.md`, its `BYPRODUCT_EXCLUDES`
      entry and its git probe (#1327). Verify: `tests/test_opencode_executor.py` (task delivery
      section), `tests/test_orch_runtime.py::test_byproduct_excludes_no_longer_name_the_opencode_context_file`.

## 6. Doctor version check

- [x] 6.1 Add a `verdict doctor` check that reads the installed OpenCode version
      and warns (non-fatal) when it differs from the pinned v2.3.2. Verify: doctor
      output shows the warning when a different version is installed; doctor stays
      green otherwise.

## 7. Docs, tests and gates

- [x] 7.1 Update `docs/guides/harnesses.md` to record OpenCode as an opt-in
      worker executor (`--executor opencode`; `direct-gateway` stays the default) and the
      route-through-serve contract. Update ETHOS.md `In
      force` or `NOW` only if the operator asks. Verify: `check_doc_links.py` and
      `check_reachability.py` are green; no host paths in fixtures or docs.
- [x] 7.2 Run the full suite, ruff, mypy --strict, reachability, the lint gates,
      both OpenSpec validators, and get a cross-family (non-Claude) review. Verify:
      all commands green; review from a different model family. **Evidence (2026-11-24):**
      at the head `cc955916`, `ruff check`, `ruff format --check`, `mypy verdict --strict`,
      `scripts/check_lint_gates.py --wip` and `git diff --check` exit 0 and the push gate
      passed; the full suite is CI's, run on the pushed head and checked at merge, not run
      locally. The cross-family review (`ocr`, `cx/gpt-6.1-sol`) of `cc955916` is complete:
      28 of 28 files reviewed, 0 failed reviewer tool calls, 1 finding. It is not a clean
      review. The finding is that the status matcher in
      `verdict/orchestration/executor_opencode.py` misses the standard `requests` forms
      `429 Client Error: Too Many Requests for url: ...` and
      `503 Server Error: Service Unavailable for url: ...`, so such an error gets no status
      and is not rerouted. The operator accepted it on 2026-11-24 and moved it to #1313,
      because the adapter was opt-in and, at the time, could not complete a request through
      serve until #1313 reworked that path (it has since: serve's side in #1335, the executor's
      in #1325). No review was run on the commit that records this decision.
- [ ] (post-merge) 8.1 On installed main, run `verdict orchestrate --executor opencode` on a
      task with a coding node (OpenCode is opt-in, so the flag is required); it needed #1313 (serve
      honours the assignment) and #1325 (the executor signs one) and needs the signing key from
      `verdict setup --apply --allow host.serve`: the node edits a file, runs
      verification, and the receipt shows `harness: opencode` with the correct route and usage
      and what OpenCode reports as cost. Record the raw output in the story's DoD evidence file.
