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
      `DEFAULT_REGISTRY` as the new default (`default=True`), keeping
      `direct-gateway` and `prime-headless` registered. Add `harness-opencode`
      capability label to the registry executor map. Verify: `verdict harness list`
      shows `opencode` as default; `direct-gateway` and `prime-headless` are still
      selectable.
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
- [ ] 2.3 Per-attempt config isolation: each attempt writes a fresh config to a
      new temp directory. The config sets the provider base URL to
      `http://127.0.0.1:8000/v1`, maps `verdict/<route_id>` to `<route_id>`, sets
      `permissions` to restrict file edits to the node worktree, and denies
      `external_directory`. The API key is not written to the config file or to
      any log. Verify: a live check confirms session DB and state are isolated
      between two concurrent workers; no credentials appear in temp config files or
      logs.
      **Not ticked (2026-11-24).** A live run through this branch's serve on a spare port
      verified the pre-dispatch checks and the launch for real, but serve admitted no FREE route:
      the pinned `openrouter/baidu/ernie-5.5-flash-preview:free` was excluded (`passport_stale`,
      `confirm_budget_exhausted`) and the three admitted routes were subscription routes, so no
      worker edit ran. The passport store is still overwritten by tests on main (#1306). Raw output:
      `/tmp/claude-takeover/oc1-smoke/run-20261124T042116Z/`. Retry on installed main after #1306.
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

- [x] 7.1 Update `docs/guides/harnesses.md` to record OpenCode as the default
      worker executor and the route-through-serve contract. Update ETHOS.md `In
      force` or `NOW` only if the operator asks. Verify: `check_doc_links.py` and
      `check_reachability.py` are green; no host paths in fixtures or docs.
- [ ] 7.2 Run the full suite, ruff, mypy --strict, reachability, the lint gates,
      both OpenSpec validators, and get a cross-family (non-Claude) review. Verify:
      all commands green; review from a different model family.

- [ ] (post-merge) 8.1 On installed main, run a `verdict orchestrate` task on a
      coding node using an OpenCode worker: the node edits a file, runs
      verification, and the receipt shows `harness: opencode` with the correct
      route and usage. Record the raw output in the story's DoD evidence file.
