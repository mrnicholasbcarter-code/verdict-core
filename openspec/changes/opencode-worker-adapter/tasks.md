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
      `DEFAULT_REGISTRY` (opt-in when it merged, selected with `--executor opencode`; the default
      since 2026-11-25, task 9.1), keeping `direct-gateway` and `prime-headless` registered. Add
      `harness-opencode` capability label to the registry executor map. Verify:
      `DEFAULT_REGISTRY.names()` lists `opencode`, `direct-gateway` and `prime-headless`
      (`tests/test_harness_registry.py`, task 9.1).
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
      failed. A second review of that commit found the proof was kept by path: a path replaced
      by, or a link retargeted at, another valid receipt database kept vouching for it. What was
      seen is now bound to the file (device and inode, `serve_evidence.SeenDatabases`), to the
      file the attempt's mark was taken of, and a file replaced while it is read is unavailable;
      written first, 7 tests failed (4 of the identity helpers, and `replaced`, `retargeted` and
      `replaced-during-the-attempt` of
      `test_what_was_seen_of_a_database_does_not_outlast_the_file`: each was a false block), and
      with the file left out of the key, the mark's file not compared, or no replaced-while-read
      check, tests failed. The accepted cost is that a signing key serve does not share, refused
      from a run's first attempt, was not recognised as a block at that commit (since #1340, task
      2.21, the pre-launch probe recognises it before launch); a serve restarted on another
      database while the orchestrator's configuration names the old file cannot be told apart.
      A third review found the mark's file compared only to decide whether an empty read was
      authoritative, so a database replaced after the mark had the old file's row watermark
      applied to it: the old code read 1 request, none waived, from a file that held 4 under the
      assignment (3 waived), and associated that file with serve. A read of a file that is not the
      one the mark was taken of is now `unavailable` before the watermark is used (a database
      first created after the mark has no identity and is still read from its first row); written
      first, 3 tests failed (`test_a_file_replaced_after_the_mark_is_unavailable_with_rows_either_side_of_its_watermark`
      for the reader and for `SeenDatabases`, and
      `test_a_file_replaced_after_the_mark_is_not_associated_with_the_serve_it_was_read_for`).
      A fourth review found the path could still be replaced after that check and before the
      connection opened it, so the old watermark was applied to the other file and the read said
      `read`, with an incomplete waiver count, for any caller of the reader that did not go
      through `SeenDatabases`. The reader now takes the file's identity before the connection and
      compares it again after the query, whether the mark has an identity or not, and a changed
      identity is `unavailable`; written first, both cases of
      `test_a_file_replaced_between_the_check_and_the_connection_is_unavailable_from_the_bare_reader`
      (a mark of a file, and a database first created after the mark) failed with `read`, and
      with the new check removed they fail again. `SeenDatabases` keeps its own check for a swap
      after the reader has returned. What is not closed: a file replaced and then put back (or
      replaced by one that takes the same device and inode, which a filesystem may reuse once the
      first is deleted) between the two looks is not seen.
- [x] 2.17 The executor-to-serve path through the real app (#1325): a loopback server runs the real
      `verdict serve` app with a recording upstream; the real executor signs and a fake worker sends
      one request; the assignment is served on the signed route and serve's evidence names the
      run, the node and the route; an expired, tampered, foreign-key or future-dated assignment
      gets 403 with no upstream call and no evidence, as the run's first assignment (the attempt
      fails, its serve receipts `unavailable`, not a block) and after serve admitted an earlier
      one (a configuration block). Verify: `tests/test_opencode_assignment_serve_path.py`.
      (At that commit a first assignment's refusal was the provider's: the attempt failed with its
      serve receipts `unavailable`. Since #1340, task 2.21, an expired, foreign-key or future-dated
      first assignment is a block before launch, by the pre-launch probe; only a tampered copy or
      a key replaced after the probe still takes the first path.)
- [x] 2.21 Pre-launch assignment probe (#1340, follow-up to #1325): after the readiness check and
      before the receipts mark and the launch, the executor sends the attempt's own assignment, as
      the bearer and never with the serve token, to `POST {serve-url}/v1/chat/completions` with the
      body `{"model": "<sentinel>"}` and no `messages` (`_probe_assignment`, `_check_assignment` in
      `verdict/orchestration/executor_opencode.py`; the sentinel holds a control character, which
      no route id of a verified assignment can). Serve verifies the assignment and refuses a model
      that differs from the assignment's route with 400 before any routing, so 400 with serve's body
      is "accepted" (a 400 that is not serve's model-check body is unproven; no decision, no proof, no upstream call, no evidence); serve's exact 403
      `authentication failed` or its 503 `worker assignments are not configured` ends the attempt
      as one `harness_configuration:` block (nothing launched, no cooldown, a message naming the
      possible causes and `verdict setup`), the run's first attempt included and without reading
      receipts, so a remote or spare serve is covered; anything else (timeout, connection error, a
      redirect, another 5xx, another 403 or 400 body, a 2xx, a 404, an answer cut short) is unproven
      and the attempt goes on as before. No redirect is followed (the probe uses `http.client`, which never follows one), the
      whole exchange is bounded by what is left of the attempt's budget (the request runs on a
      helper thread the probe stops waiting for at the deadline, shutting its socket, so neither
      an answer sent a byte at a time nor a resolver that never answers can stretch it, and a
      lookup that returns late sends nothing) and no request is started for a cancelled attempt. The receipt-based classification of a refusal
      mid-run is unchanged. Serve is not changed: it logs one WARNING (`reason=model_mismatch`) per
      probe, which is accepted and documented, and its order (the model is refused before routing)
      is now relied on and pinned. Not built: a `verdict doctor` probe of the same kind (a
      separate story). Verify: `tests/test_opencode_signed_assignment.py` (the probe section),
      `tests/test_opencode_assignment_serve_path.py` (real serve app: `test_a_first_attempt_serve_refuses_is_a_configuration_block_before_anything_is_launched`,
      `test_a_serve_with_no_signing_key_is_the_same_block_naming_setup`,
      `test_a_serve_whose_receipts_the_orchestrator_cannot_read_is_covered_by_the_probe`,
      `test_a_healthy_serve_answers_the_probe_400_and_the_probe_decides_and_sends_nothing`,
      `test_a_refusal_after_serve_admitted_an_earlier_attempt_is_a_configuration_block`),
      `tests/test_serve_worker_route_proof.py`
      (`test_the_orchestrators_assignment_probe_is_refused_before_routing_and_costs_nothing`),
      `tests/test_opencode_executor.py`
      (`test_cancelling_run_waits_for_an_assignment_probe_that_is_already_in_flight`).
      **Evidence (2026-11-25):** written first against the unchanged executor, the end-to-end
      cases failed by assertion (the first attempt of an expired, foreign-key or future-dated
      assignment, and the one with receipts the orchestrator cannot read, ended with
      `error='authentication failed'`, the provider's denial, not a `harness_configuration:`
      block; 11 of the end-to-end and serve-side tests failed in all, the other seven on the
      symbols they use), and the new unit tests failed on the missing `_check_assignment`.
      After the change each of these was applied in turn and a test failed: the 403 judged by
      status alone, the 400 judged by status alone, the 503 branch dropped, the serve token sent
      as the probe's bearer, the probe run even when the readiness check failed, an unproven
      result treated as a block, a sentinel that is a valid route id, no budget bound on the probe,
      a refusal after the deadline kept as a block, an unbounded body read (chunked answer), and
      exceptions let out of the probe; and with serve's model check disabled (`if False and ...`
      in `_relay_completion`) the serve-side pin and the healthy-serve test failed (HTTP 200, a
      routed request). Review round 1 (2026-11-25, cross-family) found three things, fixed in the
      next commit: a sized answer that ends early did not raise (`http.client` `read(n)` returns
      what it got), so a cut-off answer whose bytes were serve's complete body was read as serve's
      (red: `403-complete-json-cut-short` was `refused`, `400-complete-json-cut-short` was
      `accepted`; now the declared length must match, and with that check removed the three
      cut-short cases fail); a socket timeout restarts with every byte, so an answer sent a byte
      at a time held the probe for as long as it liked (red: `test_an_answer_that_trickles_in_is_cut_off_at_the_probes_deadline`
      for both `start` and `body` waited for the whole answer; the probe now uses `http.client`
      with a watchdog that shuts the socket at the deadline, and without the watchdog both fail;
      without the connect-phase expiry check `test_a_probe_whose_connection_used_up_its_time_sends_nothing_more`
      fails); and the docs said a bare 400 is "accepted" (it is serve's own model-check body). Review
      round 2 (2026-11-25, same reviewer, head `6145ee9d`) found three more, fixed in the commit
      after it and not re-reviewed (the review budget of two rounds was spent): an answer that
      closes the connection (`Connection: close`) makes `http.client` drop its own reference to
      the socket, so the watchdog had nothing to shut and a trickled body held the probe (red:
      `test_an_answer_that_trickles_in_is_cut_off_at_the_probes_deadline[body-close]` returned
      `refused` after the whole answer); a host-name lookup is bounded by no socket timeout and
      the watchdog could not interrupt it (red:
      `test_a_resolver_that_never_answers_cannot_hold_the_probe_past_its_deadline` waited the
      whole 30 s the resolver was held for); and the requirement text promised a whole-exchange
      bound the code did not give. The request now runs on a helper thread that the probe stops
      waiting for at the deadline (a watchdog is no longer needed); with the wait unbounded, with
      the sockets not retained, or with no check before sending, 6, 6 and 2 tests fail. The
      earlier redirect check (301, 302 and 303 of
      `test_a_redirect_is_never_followed_so_the_assignment_goes_nowhere_else` failed when the probe
      used `urllib`'s default opener) still guards the contract, and `http.client` follows none.
      The review of head `44ba9d01` (2026-11-25, cross-family) found that the deadline bounds the
      caller but not the helpers: a helper stuck in a host-name lookup outlives its probe, so
      every later attempt added a thread, and `helper.start()` could finally raise into the
      attempt. At most 8 helpers are now outstanding in the process (a bounded semaphore: a place
      is taken before the helper starts and given back when it ends); a probe that finds every
      place taken, or whose helper cannot be started, is unproven without a request. Red first:
      `test_repeated_probes_behind_a_stuck_lookup_leave_a_bounded_number_of_helpers` failed with 9
      helpers left against a limit of 8, and `test_a_helper_that_cannot_be_started_is_unproven_and_frees_its_place`
      with the `RuntimeError` from `start()`; both pass after the change. With a place not given
      back after a failed start the second fails, and with one not given back when a helper ends
      both fail.
- [x] (post-merge) 2.22 On installed main, after `verdict setup --apply --allow host.serve`: the
      installed serve answers `_probe_assignment` for an assignment signed with its own key 400
      (accepted), and for one signed with another key 403 (refused), with no model call and no
      evidence row; record the raw output in the story's DoD evidence file.
      **Evidence (2026-11-26, 00:22:12Z, installed main `c0452170`, reinstalled with `verdict setup
      --non-interactive --apply --allow host.serve` and `systemctl --user restart verdict-serve`):**
      the installed serve accepted an assignment signed with its own key and refused one signed
      with another key, with no model call. Raw output, quoted from the controller's post-merge
      evidence of that date (also in the pull request body of #1343 under "Evidence"):
      `own key -> 400 {"error":{"message":"the request's model must equal the route in the worker
      assignment","type":"invalid_request_error"}}` and `other key -> 403 {"error":{"message":
      "authentication failed","type":"authentication_error"}}`; serve's log shows `POST
      /v1/chat/completions 400`, then `403`. The story's DoD evidence file takes this output when
      it is built after merge.
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
- [x] (post-merge) 2.20 The same one-attempt smoke on installed main, after `verdict setup --apply
      --allow host.serve` has created the signing key and restarted serve: an `OpenCodeExecutor`
      attempt on an admitted route lands an edit in a scratch repository and the installed serve's
      receipt shows the pinned route. Record the raw output in the story's DoD evidence file.
      Operator decision (2026-11-25): the pull request that makes OpenCode the default (task 9.1,
      #1328) is built and reviewed before this task but merges only after it passes.
      **Evidence (2026-11-26, 00:22:28Z to 00:23:27Z, installed main `c0452170`, one attempt,
      permission of the operator of that date):** the attempt passed, so the condition for merging
      the pull request of task 9.1 is met. Route `cc/claude-haiku-5-5`, a scratch git repository in
      a temp directory, `OpenCodeExecutor()` with the default serve address (port 8000),
      `run_id=run-postmerge-2-20`, `node_id=smoke-1`, timeout 300 s. Raw output, quoted from the
      controller's post-merge evidence of that date (also in the pull request body of #1343 under
      "Evidence"): `ok: true   error: ""   model: cc/claude-haiku-5-5   harness: opencode
      stop_reason: stop   duration_seconds: 58.5`; `usage: input_tokens 267, output_tokens 452,
      cost_usd null (not_reported: opencode reported 0 for a step (no pricing for a custom
      provider)), tokens_source opencode_ndjson`; `worker_assignment: assignment_digest
      ce3eb11450a96cba2011d0d797a5766d2c3a9fb70704d612c2260bf62083765b`; `serve_receipts: status
      read, requests 3, requests_waived 3, requirement_waivers [streaming /
      verified_worker_assignment]`; scratch git diff `-    return "hi"` and `+    return "hello, "
      + name`; verification `python3 -c "from greet import greet; assert greet('verdict') ==
      'hello, verdict'"` passed. Serve's receipt database (read-only): the rows carrying the
      assignment digest have `decision.chosen_from_admit = cc/claude-haiku-5-5`, the pinned route.
      Serve's log for the attempt: `GET /v1/guidance/status 200`, `POST /v1/chat/completions 400`
      (the pre-launch probe of task 2.21, accepted), then `POST /v1/chat/completions 200` for the
      worker's requests; it also shows `GET /v1/models 401` from the worker (OpenCode asks for the
      model list with the assignment and serve refuses it; harmless, not investigated). The
      story's DoD evidence file takes this output when it is built after merge. Tasks 2.8 and 8.1
      (the `verdict orchestrate` runs) are still open.
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
- [x] 2.9 (superseded 2026-11-25 by task 9.1) `direct-gateway` stays the default worker executor;
      OpenCode is opt-in with `--executor opencode` (operator decision, 2026-11-24; ADR-053
      Decision 2 amended). The operator's condition was a live smoke that passes after #1313; task
      2.19 met it on a branch and the default was switched in its own change (#1328), so the tests
      this task named (`test_opencode_is_registered_and_selectable_but_not_the_default`,
      `test_default_executor_builds_direct_gateway_not_prime`,
      `test_opencode_executor_is_built_only_when_named`,
      `test_orchestrate_help_says_opencode_is_opt_in`) were replaced by those of task 9.1.
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
      `--executor opencode` (OpenCode was opt-in when this was written, and has since become the
      default, task 9.1; it failed until #1313 landed, because serve
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
      becomes the default only after the smoke of task 2.20 passes on installed main (the pull
      request of task 9.1 merges after it; it passed on 2026-11-26). An earlier attempt on a free route
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
      worker executor (`--executor opencode`; `direct-gateway` was the default then, task 9.4
      records the default switch) and the route-through-serve contract. Update ETHOS.md `In
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
      in #1325). The matcher has since been fixed (2026-11-25, #1336: it reads both `requests`
      forms). No review was run on the commit that records this decision.
- [ ] (post-merge) 8.1 On installed main, run `verdict orchestrate` with no `--executor` flag on a
      task with a coding node (OpenCode is the default since #1328, task 9.1; it needed #1313
      (serve honours the assignment) and #1325 (the executor signs one) and needs the signing key from
      `verdict setup --apply --allow host.serve`): the node edits a file, runs
      verification, and the receipt shows `harness: opencode` with the admitted route and usage
      and what OpenCode reports as cost. Record the raw output in the story's DoD evidence file.

## 9. OpenCode becomes the default (#1328, ADR-053 Decision 2 amended 2026-11-25)

The operator's condition (2026-11-24) was a live smoke that passes after #1313. One attempt through
a spare serve from the branch landed an edit on the pinned route (task 2.19, head `538cf004`, route
`cc/claude-haiku-5-5`). The operator decided on 2026-11-25 that this change is built and reviewed
now and merged only after the same smoke passes on installed main (task 2.20). That smoke passed
on installed main `c0452170` on 2026-11-26 (task 2.20, and the probe of task 2.22), so the hold
is lifted and the change can merge.

- [x] 9.1 Make OpenCode the default worker executor: `default=True` moves from
      `DirectGatewayAdapter` to `OpenCodeAdapter` in `DEFAULT_REGISTRY`; `--executor direct-gateway`
      still selects the previous default; `--executor-map` is unchanged (unmapped nodes use
      `--executor`). Verify: `tests/test_harness_registry.py`
      (`test_registry_resolves_production_adapters_and_aliases`,
      `test_opencode_is_the_default_and_direct_gateway_stays_selectable`),
      `tests/test_orch_executors.py` (`test_cli_parser_exposes_planner_executor_role`,
      `test_default_executor_builds_opencode_not_prime_or_direct_gateway`,
      `test_opencode_executor_is_built_when_named_or_by_default`) and
      `tests/test_mixed_executor.py` (`test_executor_map_keeps_working_under_the_default`).
      Tests that relied on the old default and were changed, not weakened: in
      `tests/test_orch_executors.py`, `test_cli_parser_exposes_planner_executor_role` (the default
      is now `opencode`; the planner assertion is kept), `test_default_executor_builds_direct_gateway_not_prime`
      (now `..._builds_opencode_not_prime_or_direct_gateway`), `test_opencode_executor_is_built_only_when_named`
      (now `..._when_named_or_by_default`: the unmapped node of a map now expects OpenCode, and a second
      case with `--executor direct-gateway` expects the direct gateway) and
      `test_orchestrate_help_says_opencode_is_opt_in` (now `..._is_the_default`); in
      `tests/test_harness_registry.py`, `test_registry_resolves_production_adapters_and_aliases`
      (the default assertion) and `test_opencode_is_registered_and_selectable_but_not_the_default`
      (replaced). Each asserted the old default; each now asserts the new one, and the old
      behaviour stays covered as an explicit `--executor direct-gateway` case.
      `scripts/live_failover_smoke.py` passes `--executor direct-gateway` so that the smoke stays
      what it was. **Evidence (2026-11-25):** the tests were run against the unchanged code
      first: 21 of the 122 tests of `tests/test_harness_registry.py`,
      `tests/test_mixed_executor.py` and `tests/test_orch_executors.py` failed; with the change all
      122 pass, and 439 pass when `tests/test_opencode_executor.py`,
      `tests/test_opencode_signed_assignment.py` and `tests/test_live_smoke_opt_in.py` are added.
- [x] 9.2 Planning is unaffected: the planner call of a run with no `--executor` goes to the
      answer-only direct gateway executor and never to OpenCode. Verify:
      `tests/test_orch_executors.py` (`test_planning_is_unaffected_by_the_default_worker_executor`,
      `test_a_run_with_no_executor_flag_plans_on_the_direct_gateway_not_opencode`); against the
      unchanged code both fail (the worker default was not OpenCode), and with the planner built
      from `--executor` instead of `--planner-executor` both fail together with
      `test_cli_builds_direct_planner_and_prime_workers`.
- [x] 9.3 A machine without the binary, the signing key, serve or serve's credential ends in one
      clear `harness_configuration:` block that names the failed check, what the default needs and
      `--executor direct-gateway`; the node is not retried and nothing falls back to another
      executor. `OpenCodeWorkerExecutor` (in `harness.py`, the executor the registry builds)
      appends one note to such a block and changes nothing else; `OpenCodeExecutor` itself is
      unchanged. Verify: `tests/test_harness_registry.py`
      (`test_a_configuration_block_names_the_way_out_of_the_default`,
      `test_the_way_out_is_added_to_configuration_blocks_only`,
      `test_the_default_executor_takes_the_ids_and_keeps_the_opencode_label`) and
      `tests/test_mixed_executor.py`
      (`test_default_executor_without_opencode_ends_in_one_clear_block_never_a_fallback`, which runs
      the real executor, `run_golden_path` and the recovery classifier in a temporary home against
      a missing binary, a missing key, a closed serve port and a missing serve credential, and
      fails if the direct gateway is dispatched). **Evidence (2026-11-25):** against the unchanged
      code the 12 tests (8 in the registry file, 4 in the mixed-executor file) failed; with the
      note added to every error instead of to a `harness_configuration:` block only, the three
      tests that check the other errors pass through unchanged fail.
- [x] 9.4 The `--executor` help, `docs/guides/harnesses.md`, `docs/CONFIGURATION.md`, the README,
      this change (proposal, design, spec delta) and ADR-053 Decision 2 say OpenCode is the default,
      what it needs and the way out, with the date and the evidence the decision rests on (task
      2.19). Verify: `tests/test_orch_executors.py::test_orchestrate_help_says_opencode_is_the_default`,
      `scripts/check_doc_links.py`, and `openspec validate opencode-worker-adapter --strict`.
