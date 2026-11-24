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
- [x] 2.5 Worker credential and pre-dispatch check: resolve serve's bearer token with
      Verdict's own code, set it in the child env only under the key name, redact it, and check
      once before launch that the credential resolves, the binary runs and serve accepts it
      (one `harness_configuration:` message, treated as a block). Verify: tests in
      `tests/test_opencode_executor.py` and `tests/test_serve_service_resolvers.py`.
- [x] 2.6 Serve address from configuration (`--serve-url`, `VERDICT_SERVE_URL`, default
      `http://127.0.0.1:8000`). Verify: `tests/test_orch_executors.py`,
      `tests/test_serve_service_resolvers.py`.
- [x] 2.7 Keep the upstream HTTP status of a structured error; never record a cost of 0.
      Verify: `tests/test_opencode_executor.py`.
- [x] 2.4 Route enforcement: verify that every model call from an OpenCode worker
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
- [ ] (post-merge) 2.8 Live smoke of the worker on an admitted route, run with
      `--executor opencode` (OpenCode is opt-in) and **expected to fail until #1313 lands**
      (serve refuses the worker's requests until it honours the worker's assignment before its
      own escalation and tier classification): one attempt through
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
      path); this task and 8.1 are expected to fail until #1313 lands, and OpenCode becomes the
      default only after this smoke passes. An earlier attempt on a free route
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

## 4. Context pack delivery

- [x] 4.1 Write the context pack and owned-files list to `.verdict-context.md`
      in the node worktree before each attempt starts. Reference the file in the
      OpenCode agent `instructions` config field. Verify: a worker that reads the
      context pack from the file is able to target the correct owned files.

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
      because the adapter is opt-in and cannot complete a request through serve until #1313
      reworks that path. No review was run on the commit that records this decision.
- [ ] (post-merge) 8.1 On installed main, run `verdict orchestrate --executor opencode` on a
      task with a coding node (OpenCode is opt-in, so the flag is required) and **expect it to
      fail until #1313 lands**: once serve admits the worker, the node edits a file, runs
      verification, and the receipt shows `harness: opencode` with the correct route and usage
      and what OpenCode reports as cost. Record the raw output in the story's DoD evidence file.
