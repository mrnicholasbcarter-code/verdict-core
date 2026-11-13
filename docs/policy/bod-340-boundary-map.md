# BOD-340 native execution boundary map

Status: evidence map for BOD-340 acceptance 1. It records what runs today; it
changes no enforcement code. Later BOD-340 cards (successor ADR/OpenSpec,
dispatch/resume enforcement, review/Done enforcement, end-to-end verification)
build on it.

Rules for reading it:

- Every row cites the function that runs and how it was shown to run. "Invoked"
  means a sentinel or trace on the installed runtime reached it.
  "Static" means only a code read or a code-graph query backs it, and it says so.
- Prose guides (`docs/guides/prime-workflow.md`, `.prime/agent/skills/*/SKILL.md`)
  describe intent. They are never cited as evidence that a check runs.
- Line numbers are deliberately absent. Cite `file:function` and confirm
  against the source.

## 1. Verified runtime and source identity

Captured 2026-11-13, 14:44–16:32 UTC.

| Component | Identity | How it was verified |
|---|---|---|
| Installed Verdict (all `verdict-*` user units) | `~/.local/share/verdict/installed-main` at `50d101180dba53db8da07ac3b01a5f3e520560a6` (detached, 0 dirty files) | `git -C … rev-parse HEAD`, `git status --porcelain`. The reflog shows a move `524410f5 → 50d10118` at 15:11:28Z, made by the swarm's `install-main`. `git diff --name-only 524410f5 50d10118` lists only `tests/test_live_candidate_shortlist.py` and `tests/test_route_ladder_parity.py`, so no boundary code changed. Every sentinel in §5 ran after 15:11Z, against `50d10118`. |
| origin/main | `50d10118` (equal to installed) | `git fetch && git rev-parse origin/main`. Latest `ci.yml` run on main: `43450052804`, `success`, head `50d10118`. |
| Installed package | `verdict-core 0.5.0`, an editable install of the installed-main worktree (`direct_url.json` → `file:///home/nick/.local/share/verdict/installed-main`, `editable: true`); Python 3.14.9 | `.venv/bin/python -m pip show verdict-core`; `.venv/bin/python -V` |
| verdict-serve | `ExecStart=installed-main/.venv/bin/python -m verdict serve --host 127.0.0.1 --port 8000`, restarted 15:11:28Z | `systemctl --user show verdict-serve.service -p ExecStart -p ActiveEnterTimestamp` |
| Hourly acceptance | `verdict-acceptance.service` (`acceptance run --notify dry-run`). Run `20261113T160014Z.json`: `status: green`, `source_sha: 50d10118`, with checks doctor, eligibility, choose, route, serve and harness-prime. **None of these checks touches the operator policy.** | `~/.verdict/acceptance/20261113T160014Z.json` |
| Operator policy source | `verdict/operator_policy/v1/{manifest,rules}.json`, digest `sha256:a9967eb0c6f92efda6ede6b5f574cbb48c6f616116841675de24dce7f3b33b51`, 10 rules | `load_policy_source()` from the installed venv |
| Prime Agent | `prime-agent 0.10.2`, Rust build. The launcher `~/.local/bin/prime-agent` (shell script) execs the ELF binary `~/.local/share/prime-agent/prime-agent` (sha256 prefix `f13e3e5ebcb1aa04`). `.prime-agent-install` says `install-rust.sh channel stable version 0.10.2`. | `prime-agent --version`, `file -L`, `sha256sum` |
| Prime runtime surface | One model tool, `python_repl`; `bash`, `edit`, `rlm.spawn` etc. are programmatic tools inside it. **No pre-tool / pre-spawn hook exists.** Prime hooks are a host-side problem, not just a Verdict one: `prime-ai-memory-import.service` describes itself as "Replay idle Prime Agent sessions into ai-memory (Prime 0.10 has no hooks)". `prime-agent package list` → "No packages installed". The only extension file is `~/.prime/agent/extensions/ai-memory-pi.ts`, a TS file the Rust build does not load as an interceptor. | `prime-agent prompt` (layer breakdown: core/usage/opinionated, plus project-context, skills-inventory, mcp-servers, environment and session-role), `prime-agent --help`, `systemctl --user cat prime-ai-memory-import.service` |
| Prime skills actually loaded | `.prime/agent/skills/{hydrate-context,verdict-dispatch,verdict-finish,verdict-proof,verdict-resume}` appear in the assembled system prompt as skill inventory: text the model may follow, not code the runtime calls. Zero of the 6 sessions under `~/.prime/agent/sessions/` mention `runtime_bridge`. | `prime-agent prompt > …; grep .prime/agent/skills/` |
| OmniRoute | 3.8.53 (`omniroute.service`, `node …/omniroute/bin/omniroute.mjs --port 20128`); `/api/health` → `ok` | `package.json`, `systemctl --user show omniroute.service`, `curl /api/health` |
| ocr (review) | `open-code-review v1.12.9 (bccbc15)` | `ocr --version` |
| ai-memory | `ai-memory 3.4.5`, `ai-memory.service` active | `ai-memory --version`, `systemctl --user is-active` |
| Push gate | `/home/nick/bin/vgate` (sha256 prefix `f7666da773e9d150`, mtime 2026-11-05) runs from `verdict-core/.git/hooks/pre-push` (`exec /home/nick/bin/vgate --hook "$@"`), shared by every worktree through the common git dir. No `core.hooksPath` is set. 151 runs in `~/.verdict/vgate/runs.jsonl`; the latest is `pass` at 14:58Z. | `cat .git/hooks/pre-push`, `git config --get core.hooksPath`, `runs.jsonl` |
| GitHub `main` protection | 15 required contexts (`type-check`, `lint`, `security`, `typescript-contract-parity`, `build`, `osv-security / osv-scan`, `client-package-smoke`, `test (3.10–3.13)`, `install-smoke (3.10–3.13)`). `strict: false`, `enforce_admins: false`, no required reviews, linear history on, force-push off, no rulesets. The `gh` identity is the repo admin. | `gh api repos/…/branches/main/protection`, `gh api …/rulesets`, `gh api repos/… --jq .permissions.admin` |
| Hermes kanban runtime (drives these cards) | Hermes Agent v0.23.1. `approvals.deny`: `*VERDICT_PREPUSH_SKIP*`, `*--no-verify*`, `*.omniroute/*.sqlite*`, `*.omniroute/*.db*`. `single_query_mode: approve`. | `hermes --version`, `~/.hermes/config.yaml` (approvals section only). Two commands in this session were blocked by these rules (see §5, S-H). |

The operator's editable checkout `/home/nick/dev/verdict-core` is on `main` at
`524410f5`, with 5 untracked dirs and its own `.venv` (Python 3.13). No unit
runs from it. The acceptance and sentinel evidence above therefore comes from
installed-main, not the editable checkout.

## 2. Entrypoint table

The lifecycle states are the ones `scripts/prime_state.py:STATES` defines
(READY → HYDRATED → IMPLEMENTING → LOCAL_VALIDATION → PROOF_COMPLETE →
REVIEW_APPROVED → PR_OPEN → CI_GREEN → MERGED → MAIN_VERIFIED → DONE), plus the
orchestrate run/node states.

"Policy gate" is `verdict.operator_policy.admit_action` (PR #1000, `e306768a`).
The code graph (codebase-memory, installed-main index, inbound callers of
`admit_action`) lists exactly four production callers:
`ready_gate.ready_decision`, `run.run_golden_path`,
`supervisor_admission.evaluate_admission` and `worker_runtime._worker_admission`.
Every other path calls none.

| ID | Entrypoint (file:function) | Who calls it unattended | Transition | Existing checks it touches today | Policy gate (admit_action) | Invocation evidence |
|---|---|---|---|---|---|---|
| E1 | `verdict/orchestration/cli.py:_orchestrate` → `verdict/orchestration/run.py:run_golden_path` (`verdict orchestrate`) | Operator; `verdict supervise` / `verdict improve` children; kanban workers (none today) | goal → plan → DAG dispatch (new run, `run_started`) | OpenSpec admission `openspec_lifecycle.admit_significant_change` (only with `--openspec-change` or a persisted block); spec digest `spec_revision_digest` vs persisted `graph.json` on resume (`SPEC_CHANGED`); `RuntimePolicy(require_review=True, require_dispatch_permit=True)`; `DispatchAuthority` (BOD-433 permits); `resolve_api_key` | **Yes**: `action="orchestrate.run"`, after `run_started` and `understand` are written and before planning | **Invoked** S1 (allow, then sentinel stop), S2 (policy unavailable), S3 (rule deny emits `operator_policy_denied`) |
| E2 | `run_golden_path` with `--resume RUN_ID` (same function, same gate) | `verdict supervise` restarts every generation with `--resume` (`supervisor.build_command_factory`) | resume: reuses `prior_validated` nodes and replans if needed | Constraints digest must equal the original `run_started.constraints_sha256`; OpenSpec digest recheck; `conformance_result` reset to `None`; per-node permit on any new send | **Yes** (same call site; resumed runs re-run it) | **Invoked** S4: three supervise generations each emitted `run_started, understand` and stopped at the gate |
| E3 | `verdict/orchestration/supervisor.py:dispatch` / `ControllerSupervisor.run` (`verdict supervise`) | Operator; `verdict improve` | spawn/restart controller generations; FAILED_CLOSED on budget/deadline | Requires a delegated cgroup (`--cgroup-root`); `controller_operations.activate_generation` / `revoke_generation` (generation token); bounded `--max-restarts`, `--total-deadline-seconds`; `preflight_worker_containment` | **No direct call**; inherits E1/E2 in each child | **Invoked** S4: 2 generations CRASHED → FAILED_CLOSED → `run_finished BLOCKED`, policy denial in both controller logs |
| E4 | `verdict/orchestration/cli.py:_improve` → `improvement.run_with_supervisor` (`verdict improve`) | Operator | measure a run → write `learned` lesson to `cycles.jsonl` → next cycle's `--constraints-file` | `verify_cycles` recompute; owned flags (`--runs-dir/--resume/--constraints-file`) refused; cgroup required | **No direct call**; inherits E1 via E3 | **Invoked** S5: run denied ×3, yet the cycle still recorded `learned controller "Split the goal into smaller nodes…"` (see B11) |
| E5 | `verdict/orchestration/runtime.py:DagRuntime._attempt` (per-node dispatch inside E1/E2) | E1/E2 | node READY → RUNNING → VALIDATED/BLOCKED (worker send) | Context budget/hydration; executor origin pin (`gateway_origin_matches`); `_bind_proof` + `_commit_permit` → `HealthCache.commit_dispatch` (single-use attempt id, epoch, MAYBE_SENT); `require_dispatch_permit` denial; `_validate` (node `verification_command`) and `_run_repo_gates`; bounded `max_attempts_per_node` | **No** (relies on E1's single run-level check) | **Invoked** offline S6 (`verdict demo`: 45 `_attempt` calls, 1 `admit_action`); permit path **invoked live** in run `/tmp/bod437-runs/20261113T032134Z` (3 `dispatch_permit` events; 29 `run:digest:node-aN` permits in `~/.verdict/health-cache.json`) |
| E6 | `verdict/orchestration/runtime.py:DagRuntime._conclude` → `_review_with_bound` → `OpenCodeReviewer` (review + integration) | E1/E2 | integrate leaf commits → independent review → `run_finished` | Implementer routes and families excluded (`exclude_routes`, `exclude_families`); bounded review timeout; skipped or `ERROR` review ⇒ BLOCKED; `--no-review` ⇒ BLOCKED | **No** | **Invoked** offline S6 (`_conclude`, `_review_with_bound` called; receipt `COMPLETE`, review `PASS`) |
| E7 | `verdict/orchestration/receipt.py:write_run_receipt` + `completion_verdict` (finish/certify a run) | E1/E2 end | run → COMPLETE/BLOCKED receipt bound to sha256 of `events.jsonl` | Receipt digest binding; COMPLETE needs validated work + an ok integration barrier + a clean PASS review; `check_conformance` with `--openspec-change` | **No** | **Invoked** S6. Note B10: a pre-plan policy block writes **no** receipt |
| E8 | `verdict/worker_runtime.py:cli_run` → `_worker_admission` → `WorkerController.run` (`python -m verdict.worker_runtime <dir>`, the direct/native worker) | Prime bridge E9; manual | worker admitted → route permit → `adapter.spawn` | Canonical live admission (`admission.admit`); Prime visibility; `require_admission=True`; `passport_confirmation`; `DispatchCache.commit_dispatch` permit; controller exclusion (`VERDICT_CONTROLLER_MODEL`, `excluded_route_ids`); route-prefix scope | **Yes**: `action="worker.admit"`, but **after** the gateway catalog/runtime GETs, `prime-agent model list` and the write of `discovery.json` (see B6) | **Invoked** S7 (allow → sentinel stop; `discovery.json` already written), S8 (policy unavailable → `FAIL_CLOSED`, 0 attempts, `discovery.json` already written) |
| E9 | `.prime/agent/skills/verdict-dispatch/scripts/runtime_bridge.py:start` → `PrimeWorkerOperation.__init__` (Prime native dispatch) | A Prime root session that loads the skill and calls it through `runpy` | HYDRATED → IMPLEMENTING (launch worker) | `scripts/prime_workflow.py:validate_dispatch` (packet shape, worktree, branch, `base_sha` ancestry, Linear `updatedAt` equality, active/unstale lease, generation, worktree/branch) before any dir/process; controller-route exclusion; `cc/`,`kr/` prefix scope at spawn; AST caller-coverage test `tests/test_prime_dispatch_gate.py::test_every_worker_runtime_launch_site_calls_validate_dispatch_first` | **No** (gate runs later in E8) | **Invoked** S9 (invalid packet → `ValueError`, 0 bash, 0 spawn, no `worker-runs/`), S10 (valid packet+lease → 1 bash launch of `verdict.worker_runtime`). **Not invoked by the installed Prime runtime itself**: no session references the bridge, and Prime cannot force a model to call it (B1) |
| E10 | Prime native tools: `python_repl` → `bash(...)`, `edit.run(...)`, `rlm.spawn(...)`, MCP servers in `~/.prime/agent/settings.json` (serena, codebase-memory-mcp, context7, playwright, ai-memory) | Any Prime root/child session, including `prime_supervisor` controllers | arbitrary process exec / file mutation / subagent spawn | **None from Verdict.** Prime's own sandbox only. | **No**, and no supported hook exists | **Static plus runtime inventory** (`prime-agent prompt`, `--help`, binary strings). There is nothing to invoke: this is the hard gap (B1) |
| E11 | `scripts/prime_supervisor.py:main` (external Prime controller supervisor) → `_check_admission` → `evaluate_admission` → `ready_decision` | Operator (guide-only); no installed unit or cron | READY → story admitted → Prime controller launch (`run_attempt`) | `supervisor.lock`; `supersede_stale_leases`; `stop_owned_daemon`; multi-story pipeline (ready/collide/governor) and per-story lock **only when `VERDICT_MULTI_STORY=on`** (default off, ADR-037) | **Only with the flag on**: `supervise.admit` and `story.ready` | **Invoked** S11 (flag off → `(None, None)`, 0 policy calls; flag on → 1 `supervise.admit` allow; flag on + corrupt policy → `admit: False, POLICY`) |
| E12 | `verdict/orchestration/ready_gate.py:ready_decision` | E11 (flag on) only | story → READY / WAIT / EXCLUDED | `automation:manual` exclusion; dependency `MAIN_VERIFIED` evidence | **Partial**: `story.ready` runs only when the story has ≥1 dependency and all are verified. The no-dependency branch returns READY before the gate (B8) | **Invoked** S12 (corrupt policy + no deps → `READY`; corrupt policy + verified dep → `WAIT: operator policy unavailable`) |
| E13 | `verdict/cli.py:cmd_autodev` → `autodev_run.run_autodev` (`verdict autodev --objective`) | Operator | decompose → execute units → verify (edits the working tree) | `--allow-live` plus `_require_launch_decision` (BOD-104 `ExecutionPathDecision`); `_assert_exact_model`; base-url pinning | **No** | **Invoked** S13 (no `--allow-live`: `AutodevError: missing ExecutionPathDecision`, 0 gate calls, 0 tree changes) |
| E14 | `verdict/cli.py:cmd_autodev_packet_execute` (`verdict autodev packet execute`) | Operator | packet → live execution in the tree | `--allow-live` required; `_cli_execution_path_decision` | **No** | **Invoked** S14 (no `--allow-live` → refusal JSON, 0 gate calls). The code graph shows 0 inbound callers other than CLI dispatch |
| E15 | `verdict/cli.py:cmd_resume` → `actions/extra._action_resume` → `resume.resume_story` (`verdict resume <story> --create`) | Operator; harness launchers | create/reattach a story worktree and branch, build a resume prompt | `worktree_registry.ensure_story_worktree` (reattach before create); no-dirty-reset prompt text | **No** | **Invoked** S15: `--create` for fake `BOD-999` created worktree `~/worktrees/bod-999` and branch `feat/bod-999-work` in a sandbox repo with 0 gate calls (cleaned up). This is `branch.create` with no issue/AC check (B9) |
| E16 | `verdict run cancel|retry <run> [node]` → `actions/registry` `run.cancel`, `run.cancel-node`, `run.retry-node` → `DagRuntime._poll_controls` / `_handle_retry_node` | Operator, TUI cockpit | node TERMINAL_FAILURE/BLOCKED → re-drive; cancel | Durable control requests; retry only for TERMINAL_FAILURE/BLOCKED; recovery budget | **No** | **Invoked** S16 for the refusal path only (`run does not have a durable graph and event log`, 0 gate calls). A live retry inside a running controller was not exercised |
| E17 | `scripts/prime_workflow.py:main` `check-proof` / `open-pr` → `check_bundle` (`validate_packet`, `validate_proof`, `validate_packet_proof`, `validate_review`, digest checks) → `gh pr create`; `merge-guard` → `merge_guard`; `lease-reap`, `status` | Prime finish/proof skills (text only); operator | PROOF_COMPLETE → REVIEW_APPROVED → PR_OPEN; MERGED (serialised) | Clean tree; head == proof head == review head; evidence sha256; `require_lease_for_pr`; remote head == local head; integration flock | **No** | **Invoked** S17 (`check-proof` on a malformed bundle → `BLOCKED: packet missing issue`, exit 1). `prime_workflow.transition` has **0 callers** (code graph), so lifecycle state names are never enforced as a state machine |
| E18 | Done/learning: `scripts/check_dod.py --all` (CI `lint` job); `improvement` `learned` stage; `prime_workflow.transition(…, "DONE")` (uncalled) | CI; E4 | MAIN_VERIFIED → DONE; lesson written | DoD schema: merged SHA ancestry, raw-output digests, freshness window, setup/doctor green, archived OpenSpec, a parent confirmation by a non-author | **No** | **Invoked** S18 on installed-main: `check_dod: 0 evidence file(s) checked`, exit 0 (no `docs/proof/dod/` exists yet) |
| E19 | `POST /v1/route`, `/v1/chat/completions`, `/v1/responses` (`verdict/api.py`, `verdict serve`) | Harnesses through `verdict-serve.service` | route/relay one model call | Bearer auth; `execution_path_request` required on `/v1/route`; relay `idempotency_key` and `retry_safety`; permit `revocation_epoch` recheck in streaming; `stream_terminal` close-once | **No**. These are model-call boundaries, not lifecycle boundaries; listed for completeness | **Invoked** S19 (`POST /v1/route` without auth → `401 authentication required`) |
| E20 | Git `pre-push` → `/home/nick/bin/vgate --hook` | Every `git push` from any verdict-core worktree | branch → remote | clean tree, ruff check/format, mypy --strict, doc links, contract schema, proof matrix, test collection, vendored/host-path guards, bandit, targeted tests; `VERDICT_PREPUSH_SKIP=1` emergency switch (logged) | **No** (vgate is operator-owned and must not be edited) | **Invoked**: 151 entries in `~/.verdict/vgate/runs.jsonl`, the latest `pass` at 14:58Z (head `380baa8c`) |
| E21 | GitHub required checks on `main` | Every PR to main | PR → mergeable | 15 required contexts (§1); `ci.yml` `lint` job also runs `check_reachability --ratchet-base`, `check_dod --all`, `openspec validate --all --strict`, `check_openspec_tasks.py` | **No** | **Invoked**: main CI run `43450052804` success on `50d10118` |
| E22 | Hermes kanban dispatcher → card worker (this card's runtime) | Hermes gateway | board card → agent session → tool calls (bash, git, `gh`) | `approvals.deny` globs (above); Verdict's own gates only if the worker calls Verdict commands | **No** | **Invoked** S-H: two sentinel commands were refused by the deny globs only because their text happened to contain the forbidden strings |

Test/demo-only callers of `admit_action` (code graph): `scripts/demo_orchestrate.py`,
`scripts/demo_scenario.py`, `scripts/record_tui_demo.py`, and
`orchestration/demo_scenario.run_flagship_scenario` (`verdict demo`). They
go through E1 with scripted executors.

## 3. Existing-control inventory

| Control | Where it lives | Scope | Invoked on installed runtime? |
|---|---|---|---|
| Approved policy source + digest | `verdict/operator_policy/__init__.py:load_policy_source`; `v1/manifest.json` digest over `rules.json` bytes | E1/E2, E8, E11/E12 (flag on) | Yes (S2/S8: `digest mismatch: rules altered since approval`) |
| Policy evaluation | `operator_policy:evaluate_action` (first match by precedence, default deny `no_rule_authorizes_action`) | same | Yes (S3) |
| Override tokens (single-use nonce, expiry, operator, action/scope, digest) | `operator_policy:check_override_token`; nonce store `_MemoryNonceStore` (**process-local, not durable**) | `admit_action(override_token=…)`. **No production caller passes a token.** | Exercised directly (S20). The replay check works only within one process |
| Secret-free policy audit | `operator_policy:audit_record` (fingerprints via `verdict.security.fingerprint_text`, `redact_text`) | returned by `admit_action` | Returned, but **not persisted**: only E1's deny emits `operator_policy_denied` with `decision_id`; allows and E8/E11 decisions are dropped |
| Packet/lease dispatch gate | `scripts/prime_workflow.py:validate_dispatch`, `validate_packet`; `scripts/prime_state.py:acquire_lease`, `heartbeat_lease`, `release_lease`, `supersede_stale_leases`, `require_lease_for_pr` (generation fence) | E9, E17 | Yes (S9/S10/S17) |
| Packet → worker binding | the bridge writes `packet`, `attempt_id`, `lease_generation` into `config.json` | E9 → E8 | Written (S10), but **no code in `verdict/` reads `lease_generation`** (grep: 0 readers) |
| Dispatch permits (single-use attempt id, epoch, MAYBE_SENT) | `verdict/orchestration/health_cache.py:HealthCache.commit_dispatch`, `revocation_epoch`, `permit_for_attempt`; `relay.admission_proof`, `relay.dispatch_scope`; `runtime.permit_attempt_id` (run-dir digest namespacing); `PERMIT_PROOF_MAX_VALIDITY_SECONDS = 600` | E5, E8, E19 | Yes (237 permits in `~/.verdict/health-cache.json` `dispatch.attempts` at 16:4xZ, all `maybe_sent`; 29 orchestrate-style `run:digest:node-aN`) |
| Revocation epoch recheck on stream | `verdict/api.py` (relay paths compare `revocation_epoch(proof.scope)` with `permit.epoch`) | E19 | Static only in this map (BOD-434 evidence covers it) |
| Controller generation tokens / revocation | `verdict/orchestration/controller_operations.py:activate_generation`, `bind_controller`, `revoke_generation`, `OperationLeaseGuard`, `owned_worker_command`, `register_worker`, `scrub_fencing_env` | E3/E4, E5 | Yes (S4: `controller-operations.json`, REPLACED/FAILED_CLOSED) |
| Worker containment (cgroup v2) | `controller_operations.prepare_containment`, `join_containment`, `preflight_worker_containment` | E3/E4 | Yes (S4/S5 needed `systemd-run --user --scope -p Delegate=yes … --cgroup-root auto`) |
| Bounded attempts / deadlines | `RuntimePolicy` (`max_attempts_per_node`, `attempt_timeout_seconds ≤ 900`, `run_deadline_seconds`), supervisor `max_restarts`, `total_deadline_seconds` | E1–E5 | Yes (S4 restart budget exhausted → FAILED_CLOSED) |
| Review independence | `runtime._conclude` (implementer route/family exclusion), `review.OpenCodeReviewer`, `completion_verdict` | E6/E7 | Yes (S6) |
| Receipt digest binding | `orchestration/receipt.py:write_run_receipt`, `completion_verdict`; `tests/test_orch_receipt.py` | E7 | Yes (S6) |
| OpenSpec admission / spec digest / conformance | `verdict/openspec_lifecycle.py:admit_significant_change`, `spec_revision_digest`, `check_conformance`; `run.py` `SPEC_CHANGED` path | E1/E2 only when `--openspec-change` or a persisted block exists | Static here (S1–S5 ran without `--openspec-change`) |
| Idempotency | relay `idempotency-key` header (`verdict/relay.py:idempotency_key`, `retry_safety`); `memory_outbox.MemoryOutbox` (`idempotency_key` PK); `execution_packet` transition keys; `claims_ledger` | E19, memory capture | Static here |
| SSE terminal / close-once | `verdict/stream_terminal.py` (BOD-434) | E19 | Static here (BOD-434 Done) |
| Secret protection | `verdict/security.py:redact_text`, `fingerprint_text`; `shared_memory.redact_secrets`; `memory_gate._redact_value`; vgate bandit + host-path guard; CI `security` job (bandit, pip-audit); `tests/conftest.py` credential stripping | all | vgate/CI invoked (E20/E21) |
| Push gate | `/home/nick/bin/vgate` via `.git/hooks/pre-push` | E20 | Yes |
| Hermes approvals deny list | `~/.hermes/config.yaml` `approvals.deny` | E22 only | Yes (S-H) |
| Demo output guard | `scripts/demo_orchestrate.py:main` refuses `--out` inside the repo unless `--update-proof` | demo | Yes (S21: exit 2, nothing created) |
| Caller-coverage AST test | `tests/test_prime_dispatch_gate.py` (`UNGATED_ALLOWED = {subagent_selection.execute_with_worker_failover}`) | CI | CI only. It checks `validate_dispatch` coverage, not `admit_action` coverage |
| Reachability ratchet | `scripts/check_reachability.py --ratchet-base`; `reachability-baseline.json` (185 entries; `enforcement.EnforcementVerificationGate`, `enforcement.dispatch_with_enforcement` and `memory_gate.MemoryGovernor` are UNTRIAGED with no production caller; `supervisor_admission.acquire_integration_lock` is KEEP, test-only) | CI | CI |

Existing code that is **not** a live boundary today and must not be counted
as one: `verdict/enforcement.py:EnforcementVerificationGate` and
`dispatch_with_enforcement` (no production caller); `prime_workflow.transition`
(0 callers); `supervisor_admission.acquire_integration_lock` (tests only);
multi-story admission in `prime_supervisor` (flag off by default).

## 4. Bypass matrix (current status on installed `50d10118`)

"Blocked" means a sentinel showed the action refused before its side effect.
"Open" means a sentinel or a traced code path showed it succeeds. "Partial"
means blocked on some entrypoints and open on others.

| ID | Bypass | Status | Evidence |
|---|---|---|---|
| B1 | Prime native tool calls (`bash`, `edit`, `rlm.spawn`, MCP tools) outside any Verdict boundary: process exec, file mutation, subagent dispatch | **Open**: no supported pre-action hook in Prime 0.10.2 (Rust) | §1 Prime rows; E10. BOD-340 AC1 requires a version-pinned adapter/patch or "doctor red/unadmitted"; neither exists. `verdict doctor` has 0 references to `operator_policy` |
| B2 | A Prime session launches `python -m verdict.worker_runtime <dir>` directly, skipping `runtime_bridge.start`, so no packet/lease check runs | **Partial**: E8's `worker.admit` still runs, but it allows with no issue/AC/brief (B4) | S7 |
| B3 | Goal-mode `verdict orchestrate`/`supervise`/`improve` with no issue, AC, packet, lease or spec | **Open**: `pol.allow-lifecycle-admission` allows `orchestrate.run` unconditionally | S1 (allow with no issue), §5 table row "orchestrate.run no issue → allow" |
| B4 | Worker admission without linear_issue / AC / brief / ownership / safety / proof refs | **Open**: same allow rule. The bridge's `config.json` task carries only `allowed_route_prefixes` and `excluded_route_ids` (S10). The fields **cannot** be supplied anyway: `worker_task_from_config` builds `WorkerTask(**config["task"])` before `_worker_admission` reads `task.linear_issue`, and `WorkerTask` rejects the key, so a config that carries them fails closed with a `TypeError` and the gate is never reached | S7, S10, S7b |
| B5 | `subagent.dispatch` / `source.edit` / `branch.create` actions | **Not wired**: no production caller emits these actions. The rules exist, but even a fully compliant request is DENY `override_required` (no allow path besides an override), so wiring them as-is would block all legitimate work | §5 policy table; `tests/test_operator_policy.py::test_source_edit_requires_issue_and_ac`, `::test_subagent_dispatch_requires_cited_brief` assert this |
| B6 | Side effects before the worker gate decides | **Open (bounded)**: `cli_run` does gateway GETs, `prime-agent model list`, and writes `discovery.json` before `_worker_admission` | S8 (deny still left `discovery.json`) |
| B7 | Side effects before the orchestrate gate decides | **Open (bounded)**: `run_started` + `understand` events, `progress.json`, run dir, and `build_selector`'s `admission-latest.json` write all happen before the gate | S2/S3 |
| B8 | `ready_decision` with zero dependencies skips the policy gate | **Open** | S12 |
| B9 | `verdict resume --create` creates a branch and worktree with no issue/AC/policy check | **Open** | S15 |
| B10 | A pre-plan policy block leaves no receipt and no `run_finished`, so `verdict supervise` reads it as a crash and restarts the denied run up to `--max-restarts` | **Open (fails closed eventually)** | S2 (no receipt), S4 (CRASHED → REPLACED → CRASHED → FAILED_CLOSED) |
| B11 | `verdict improve` records a `learned` lesson from a policy-denied run | **Open** | S5 (`learned controller …` after 3 denials) |
| B12 | `VERDICT_OPERATOR_POLICY_DIR` points at a self-written policy with a self-computed digest | **Open**: the digest only proves internal consistency, not approval | S20a (`gate.mutate` allowed by `sentinel.allow-everything`) |
| B13 | Self-issued override token (no signature; the operator id is plain text) | **Open**: accepted | S20b (`override_accepted:pol.require-cited-brief-dispatch`) |
| B14 | Override token replay across processes (nonce store is process-local) | **Open** | S20b: the same nonce was accepted in two processes; replay was rejected only within one process |
| B15 | Policy audit not persisted (allow decisions, worker/supervise decisions) | **Open** | §3 audit row |
| B16 | `git push --no-verify` / `-n` / `-c core.hooksPath=…` / `VERDICT_PREPUSH_SKIP=1` | **Partial**: vgate does not run under `--no-verify` or `core.hooksPath` (sandbox S22: hook-denied push succeeds with `-c core.hooksPath=/dev/null`). Hermes `approvals.deny` blocks two literal strings for kanban agents only. `admit_action(git.push)` allows `-n` and `-c core.hooksPath` because the rule matches only the literal `--no-verify`, and no production caller sends `git.push` anyway. GitHub required checks still gate merges to `main`, but `enforce_admins: false` and the token is an admin | S22, §5 policy table |
| B17 | vgate mutation through a wrapper (`sh -c 'echo > /home/nick/bin/vgate'`) | **Partial**: `gate.mutate` matches only argv0 `vgate`; the wrapper case falls through to the default deny, but only if a caller classifies it as `gate.mutate`, and none does. The file is user-writable (`-rwxrwxr-x nick`) | §5 policy table |
| B18 | Service restart of non-listed units (e.g. `verdict-acceptance.service`) or `kill <pid>` of running work | **Partial**: denied only by the default `no_rule_authorizes_action`, and only if a caller emits the action; none does | §5 policy table |
| B19 | `scripts/demo_orchestrate.py` writing into the repository | **Blocked** (script-local guard, not policy) | S21 |
| B20 | `prime_supervisor` multi-story admission off by default | **Open by design** (ADR-037): with the flag off, neither `supervise.admit` nor `story.ready` runs | S11 |
| B21 | Corrupt/tampered shipped policy | **Blocked** at E1/E2/E3/E4/E8/E11 | S2, S4, S8, S11 |
| B22 | Orchestrate worker send without a route permit | **Blocked** (`require_dispatch_permit=True` + `DispatchAuthority`) | E5 live run permits; `tests/test_orch_runtime.py` |
| B23 | Opaque `auto/*`, `combo/*` routes; skipped/empty review | **Blocked** (admission and review invariants) | AGENTS.md invariant tests; S6 review PASS required for COMPLETE |

## 5. Sentinel log

All sentinels used isolated `VERDICT_HOME` (`$TMPDIR/bod340/vhome*`), temp git
repos, and either a sentinel exception raised right after the gate allowed or a
route scope (`sentinel-nonexistent/`) that cannot match. No model was called; no
real `~/.verdict`, repo, service or vgate file was modified. The tracer
(`trace_gate.py`) hooks `sys.settrace` on the installed
`verdict/operator_policy/__init__.py:admit_action` and counts real calls.
Raw outputs and scripts are preserved at
`~/dev/verdict-core-worktrees/packs/m-swarm-20261112/stories/BOD-340/boundary-map-evidence-20261113/`
(and `.tgz` beside it). They are kept outside `docs/proof/` deliberately,
because these are sandbox fixtures, not live acceptance evidence.

| # | Command (installed-main venv, `env -i`) | Result |
|---|---|---|
| S1 | `trace_gate.py verdict orchestrate --graph sentinel.json --no-review` with trip-after-allow | 1 `admit_action(orchestrate.run)` → allow `pol.allow-lifecycle-admission`; stopped; events so far `run_started, understand`; repo unchanged |
| S2 | same + `VERDICT_OPERATOR_POLICY_DIR=policy-corrupt` | `VERDICT BLOCKED: operator policy unavailable: operator policy digest mismatch…`; events `run_started, understand`; **no receipt, no `run_finished`** |
| S3 | same + `policy-norule` (valid digest, no lifecycle allow) | `VERDICT BLOCKED: operator policy denied: no_rule_authorizes_action`; events `run_started, understand, operator_policy_denied` |
| S4 | `systemd-run --user --scope -p Delegate=yes … verdict supervise --cgroup-root auto --run-id SUPDENY --max-restarts 1 -- --graph … ` + corrupt policy | g0 CRASHED → REPLACED → g1 CRASHED → FAILED_CLOSED → `run_finished BLOCKED "controller CRASHED and restart budget (1) exhausted"`; both controller logs show the policy denial |
| S5 | `verdict improve --once … -- --graph …` + `policy-norule` | 3× `operator_policy_denied`; cycle `NOT_MET` "missing receipt.json"; **`learned controller "Split the goal into smaller nodes…"`** |
| S6 | `trace_multi.py verdict demo --json --worker-seconds 0` | calls: `admit_action` 1, `_attempt` 45, `_conclude` 3, `_review_with_bound` 3, `completion_verdict` 3, `write_run_receipt` 1, `_commit_permit` 0 (offline demo has no `DispatchAuthority`); receipt `COMPLETE`, review `PASS` |
| S7 | `trace_gate.py verdict.worker_runtime wdir` (trip-after-allow) | 1 `admit_action(worker.admit)` → allow with no issue/AC/brief; `discovery.json` already written |
| S7b | same, with `task` carrying `linear_issue`, `acceptance_criteria`, `brief_refs`, `ownership_refs`, `safety_refs`, `proof_refs` | `FAIL_CLOSED … WorkerTask.__init__() got an unexpected keyword argument 'linear_issue'`; 0 gate calls |
| S8 | same + corrupt policy | outcome `FAIL_CLOSED … operator policy unavailable…`, 0 attempts; `discovery.json` written before the denial |
| S9 | `bridge_probe.py` (bridge `start` with a structurally incomplete packet) | `ValueError packet missing acceptance_criteria`; bash 0, spawn 0, no `worker-runs/`, `.git` unchanged |
| S10 | `bridge_allow.py` (valid packet + active lease, fake `bash` handle) | 1 bash launch of `.venv/bin/python -m verdict.worker_runtime …`; `config.json` keys `attempt_id, budget, controller_model, lease_generation, packet, prompt, task`; task has **no** `linear_issue/acceptance_criteria/brief_refs/ownership_refs/safety_refs/proof_refs` |
| S11 | `sup_probe.py` → `prime_supervisor._check_admission` | flag off → `(None, None)`, 0 policy calls; flag on → `supervise.admit` allow; flag on + corrupt → `{'admit': False, 'reason_code': 'POLICY', …}` |
| S12 | `ready_decision(deps=[])` and `ready_decision(deps=[verified])`, corrupt policy | `READY "no dependencies; story is ready"` vs `WAIT "operator policy unavailable…"` |
| S13 | `verdict autodev --objective sentinel --json` (no `--allow-live`), corrupt policy | `AutodevError: missing ExecutionPathDecision`; 0 gate calls |
| S14 | `verdict autodev packet execute --packet bogus --repo sandbox --json` | `pass --allow-live` refusal; 0 gate calls |
| S15 | `verdict resume BOD-999 --repo sandbox --create --json` (`HOME` = sandbox) | worktree `…/fakehome/worktrees/bod-999` + branch `feat/bod-999-work` created; 0 gate calls (removed afterwards) |
| S16 | `verdict run retry NO-SUCH-RUN n1 --runs-dir …` | `refused: run does not have a durable graph and event log`; 0 gate calls |
| S17 | `prime_workflow.py check-proof` on a malformed bundle | `BLOCKED: packet missing issue`, exit 1 |
| S18 | `scripts/check_dod.py --all` (installed-main) | `check_dod: 0 evidence file(s) checked`, exit 0 |
| S19 | `curl -X POST 127.0.0.1:8000/v1/route` (no auth) | `401 authentication required` |
| S20a | `load_policy_source()` with `VERDICT_OPERATOR_POLICY_DIR=policy-self` | `gate.mutate vgate` → allow `sentinel.allow-everything` |
| S20b | `token_probe.py` in two processes with the same self-built token | both `override_accepted:pol.require-cited-brief-dispatch`; in-process replay rejected `override_replay_or_reused_nonce` |
| S21 | `scripts/demo_orchestrate.py --out <installed-main>/bod340-sentinel-out` | exit 2 "inside the repository"; nothing created; installed-main still clean |
| S22 | sandbox bare remote, `pre-push` hook that always fails | normal push exit 1 (hook ran); `git -c core.hooksPath=/dev/null push` exit 0 (hook skipped, commit landed) |
| S-H | two python heredocs whose source text contained the literal strings `VERDICT_PREPUSH_SKIP` / `--no-verify` | refused by Hermes `approvals.deny` before execution (string match on command text, not semantics) |

Policy evaluation on the shipped source (installed venv, `admit_action(…, source=load_policy_source())`):

| ActionContext | Decision |
|---|---|
| `story.ready`, `orchestrate.run` (no issue), `worker.admit` (no issue/brief) | allow `pol.allow-lifecycle-admission` |
| `source.edit` with issue+AC | deny `pol.require-tracked-issue-ac`, `override_required` |
| `source.edit` without issue | deny `no_rule_authorizes_action` |
| `subagent.dispatch` with brief+ownership+safety+proof refs | deny `pol.require-cited-brief-dispatch`, `override_required` |
| `subagent.dispatch` without refs | deny `no_rule_authorizes_action` |
| `git.push origin x`, `git.push -n`, `git -c core.hooksPath=/dev/null push` | allow `pol.allow-git-push-through-gate` |
| `gate.mutate` argv0 `/home/nick/bin/vgate` | deny `vgate_mutation_protected` |
| `gate.mutate` via `sh -c 'echo x > …/vgate'` | deny `no_rule_authorizes_action` |
| `service.restart systemctl --user restart omniroute.service` | deny `unauthorized_service_restart` |
| `service.restart … verdict-acceptance.service`; `process.kill kill -9 12345`; `file.write` | deny `no_rule_authorizes_action` |

## 6. Dependency interfaces enforcement must consume

| Story | Interface on main today | Interface pending (not merged) | What BOD-340 must call, not rebuild |
|---|---|---|---|
| BOD-303 packet/lifecycle (Linear Backlog) | `scripts/prime_workflow.py:validate_packet` (presence/shape only: `issue_updated_at`, `spec`, `plan`, `lease`, `acceptance_criteria[{id,text,verification}]`, `dependencies[SATISFIED]`, `proof_requirements` = `GATES`, `budgets`, `context` dict); `validate_dispatch`; `validate_packet_proof`; `prime_state` leases/checkpoints; `verdict/execution_packet.py` (packet ledger + transitions); OpenSpec `bod-303-worker-context-packs` and `bod-303-selected-context-bridge` (unarchived; 3 and 1 open tasks) | AC→scenario→test mapping and ADR disposition fields are not in the packet; no packet field carries spec/ADR revision ids | Validate story/spec/ADR revisions by extending `validate_packet`/`validate_dispatch` fields; do not add a second packet validator. Bind `config.json.lease_generation` and `attempt_id` into `_worker_admission` |
| BOD-306 mandatory retrieval (Backlog) | `verdict/context_sources.py:SharedMemoryCapabilityProvider.provide` (gap reasons `healthy_zero_hits`, `provider_auth_failed`, `provider_schema_incompatible`, `provider_timeout`, `provider_unreachable`, `shared_memory_disabled`, `provider_degraded`); `worker_context.build_worker_context`; `memory_outbox.MemoryOutbox` | Frozen contract `openspec/changes/bod-306-brief-continuity-contract` (branch `wt/t_8d484f23` @ `6adb51a4`, not on main) and implementation branches `wt/t_9f5201a6` (memory_brief), `wt/t_83449cd6` (continuity_packet), `wt/t_8a2d92f7` (memory_dispatch_gate; it touches `run.py`, `worker_runtime.py`, `supervisor.py`), `wt/t_ed3d23d3` (drills). No PRs open | Consume `memory_brief.compile_memory_brief(...).status ∈ {cited, empty, unavailable}` and `memory_dispatch_gate` refusal at E1/E2/E8. **Hotspot:** `run.py`, `worker_runtime.py`, `supervisor.py` are owned by the BOD-306 gate slice; coordinate before editing |
| BOD-336 mandatory ai-memory dependency (Backlog) | ai-memory 3.4.5 running; no Verdict admission check of it (0 references in `run.py`, `worker_runtime.py`, `supervisor.py`, `runtime.py`) | BOD-336 AC2: refuse new work when memory is absent/unauthorized/incompatible, before dispatch; running work survives | Same seam as BOD-306's `memory_dispatch_gate`; one check, not two |
| BOD-339 Prime native ai-memory (Linear **Done**) | Prime extension/backfill delivered by host-side import (`prime-ai-memory-import.service`, every 5 min) because "Prime 0.10 has no hooks" | n/a | Confirms Prime 0.10 has no supported pre-tool hook. BOD-340 AC1 must deliver a version-pinned adapter/patch or report Prime unsupported (doctor red / unadmitted). Do not claim native interception |
| BOD-96 / BOD-316 | `prime_state` lease/proof discipline; `check_reachability.py`, `check_dod.py` | — | Reuse for caller coverage and DoD; do not add a parallel reachability or DoD checker |

## 7. Gap list against BOD-340 acceptance items 2–3

Each gap names the entrypoint, the missing check, the existing code to extend,
and the sentinel that must flip from allow to deny.

### Acceptance 2: dispatch/resume boundaries

| # | Entrypoint | Required check | Today | Where to implement (reuse) | Flip sentinel |
|---|---|---|---|---|---|
| G1 | E1/E2 orchestrate, E3/E4 via children | story/spec/ADR revisions | Spec digest only, and only with `--openspec-change`; no Linear revision, no AC revision, no ADR id/status | Require an OpenSpec change (or a reviewed no-change disposition) for `orchestrate.run`; carry `linear_issue`, `issue_updated_at`, AC ids and ADR `{id,status}` in `openspec_block` / graph and recheck on resume beside `SPEC_CHANGED` | S1 must deny without issue/AC/spec |
| G2 | E1/E2, E8, E9 | semantic disposition | Not represented anywhere in code | Add a `disposition` field to the BOD-303 packet (`validate_packet`) and to `openspec_block`; reject a missing one | new: packet without disposition → `validate_dispatch` ValueError |
| G3 | E1/E2, E8 | mandatory retrieval completed | None on main | Call BOD-306 `memory_dispatch_gate` (pending branch `wt/t_8a2d92f7`) before planning (E1) and before `_worker_admission` returns (E8); `unavailable` refuses new work only | S1/S7 with ai-memory unreachable → deny, 0 model calls |
| G4 | E8 direct worker, E9 bridge | owned scope + lease bound to the worker | Bridge checks the lease; the worker never reads `lease_generation`/`attempt_id`; direct `verdict.worker_runtime` has no lease | In `_worker_admission`, load `config["packet"]`, rerun `validate_dispatch` (same function the bridge uses), and compare `lease_generation`; deny when absent | S7 must deny (no packet); S10 must still allow |
| G5 | E1/E2 | owned scope/lease for orchestrate runs | None (worktrees are runtime-owned; no story lease) | Reuse `prime_state.acquire_lease` keyed on the story; supervisor E3 holds it across generations | new: second orchestrate on same story → deny |
| G6 | E1/E2, E8 | live capable route | Exists: E5 permits (`commit_dispatch`, `revocation_epoch`) and E8 `passport_confirmation` + `commit_dispatch` | Keep as-is. Do not rebuild. Record the permit id in the policy audit | — |
| G7 | E1/E2, E8, E11 | bound authorization | `pol.allow-lifecycle-admission` allows all four lifecycle actions with no conditions. For E8, the fields `_worker_admission` reads cannot even be supplied: `worker_task_from_config` rejects them (S7b) | Read the policy fields from `config["task"]` (or a sibling `config["authority"]` block) **before** `WorkerTask(**…)`, or add them to `WorkerTask`. Then change rules so `orchestrate.run`/`worker.admit`/`supervise.admit`/`story.ready` require `requires_linear_issue` + `requires_acceptance_criteria` (+ brief/ownership/safety/proof refs for `worker.admit`); populate those fields from the packet (E9 → `config.json.task`) and `openspec_block` (E1). Approve the new digest through the operator | S1, S7, S11 must deny without refs |
| G8 | E12 `ready_decision` | policy gate on every READY | Skipped when `deps` is empty | Move the `admit_action` call above the "no dependencies" return | S12 no-deps case must return WAIT on corrupt policy |
| G9 | E8 | denial before side effects | `discovery.json`, gateway GETs and `prime-agent model list` precede the gate | Call `_worker_admission`'s policy part first in `cli_run`, before `transport.catalog()` | S8 must leave only `outcome.json` |
| G10 | E1/E2 | denial before side effects, and a settled denial | `run_started`, `understand` and `admission-latest.json` precede the gate; denial writes no receipt and no `run_finished` | Move the gate to the start of `run_golden_path` (before `run_started`), or emit `run_finished BLOCKED` + `write_run_receipt` on denial so E3 does not restart a denied run | S2 must produce `run_finished BLOCKED` and a receipt; S4 must show 1 generation, not a restart |
| G11 | E15 `verdict resume --create` | `branch.create` needs issue + AC | No gate | Call `admit_action(ActionContext(action="branch.create", linear_issue=…, acceptance_criteria=…))` in `resume.resume_story` before `ensure_story_worktree` creates | S15 must deny for an unknown issue with 0 worktrees created |
| G12 | E13/E14 autodev, E16 run retry | lifecycle policy | Neither calls `admit_action` (autodev relies on the BOD-104 launch decision) | Add an `autodev.run` / `run.retry` action through the same `admit_action`; keep the BOD-104 decision | S13/S14/S16 call counts must become 1 |
| G13 | E9 → E10 (Prime) | subagent dispatch with prepared brief | Bridge validates the packet, but `rlm.spawn` calls made from a Prime session outside the bridge are unchecked | No Verdict-side fix is possible. Ship a pinned Prime adapter/patch (BOD-340 AC1), or make `verdict doctor` report "Prime native boundary: UNSUPPORTED (red)" and refuse to label any Prime run enforced | doctor row red until an adapter exists |
| G14 | policy rules | `source.edit` / `branch.create` / `subagent.dispatch` allow path | Compliant requests still DENY `override_required`, which would block legitimate work once wired | Fix precedence so `pol.allow-source-edit-with-issue` (20) applies before the deny (10), or have the deny match only missing refs. This is an operator decision; needs a reviewed digest | policy table rows must become allow with issue+AC |
| G15 | policy source authority | approved, not just self-consistent | Any dir with a self-computed digest passes (`VERDICT_OPERATOR_POLICY_DIR`) | Pin the approved digest in code or the installed doctor/setup record and reject others; or drop the env override outside tests | S20a must fail closed |
| G16 | override tokens | operator-bound, single-use across processes | Unsigned, self-issuable; nonce store in memory | Signed token (operator key held outside the agent) + durable nonce store (e.g. the existing `HealthCache` lock/table pattern); reject self-issued | S20b second process must reject; self-built token must reject |
| G17 | audit | every allow/deny persisted | Only E1 deny emits an event | Append `audit_record` to the run `events.jsonl` (E1), the worker `events.jsonl` (E8) and the supervisor state (E11) | allow paths show a `operator_policy_decision` event |

### Acceptance 2 (second half): review, integration and Done

| # | Entrypoint | Required check | Today | Where to implement (reuse) |
|---|---|---|---|---|
| G18 | E6 review / E7 finish (orchestrate) | same packet + exact tested source | Review binds `base_ref`/`head_ref` = integration commit inside one run; no packet identity; a resumed run reuses `prior_validated` commits without revalidating packet/spec revisions | Record the packet/spec/ADR revision digest at dispatch; `_conclude` and `completion_verdict` compare it to the current one and BLOCK on mismatch (invalidate only the affected nodes) |
| G19 | E17 `open-pr` / `merge-guard` (Prime) | same packet + exact head | `check_bundle` binds proof/review/packet to HEAD, but the packet's issue/spec revision is not rechecked against Linear/spec at PR time; `merge-guard` runs any command with no head check | Call `validate_dispatch`'s revision checks from `check_bundle`; make `merge-guard` require `--match-head-commit <proof head>` |
| G20 | E18 Done/learning | independent whole-task acceptance + installed-main acceptance | `check_dod.py` validates evidence files (0 exist); `prime_workflow.transition` (0 callers); `improvement` writes `learned` lessons from denied/blocked runs (S5) | Wire `transition()` into the finish/closeout path, or retire it; require a passing `check_dod.py <story>` before any `DONE` transition; skip the `learned` stage when the run was blocked by policy (B11) |
| G21 | hourly acceptance | stays-true for policy | `verdict acceptance` checks do not include the policy boundary | Add one acceptance check that runs S2/S3-style sentinels against installed-main (isolated home, no model calls) |
| G22 | doctor | real installed boundary state | `verdict doctor` has no policy/native-boundary row | Add rows: policy digest == approved digest; Prime native boundary supported/unsupported; vgate hook wired |

### Acceptance 3: rejection rules

| # | Rule | Today | Where to implement |
|---|---|---|---|
| G23 | Missing material OpenSpec/ADR | `admit_significant_change` rejects a missing change only when one was requested; goal-mode runs need none (S1) | Make an OpenSpec change (or reviewed no-change disposition) mandatory for `orchestrate.run` / `worker.admit` on code-changing work |
| G24 | Superseded ADR | No code reads ADR status (grep: only lease/claims "superseded") | Add an ADR index reader over `docs/adr/*` status lines, used by the packet validator |
| G25 | Code/spec/ADR conflict | None | Same ADR reader + OpenSpec `check_conformance`; conflict → reject |
| G26 | Proposed represented as Accepted | None | Packet carries `{adr_id, status}`; validator compares it with the file's `Status:` line |
| G27 | Unauthorized weakened scenario/assertion | None at dispatch/review (vgate/CI run tests but do not detect weakening) | Diff-based check in review (E6/E17): removed or loosened `#### Scenario` / `assert` without a linked reviewed disposition → BLOCK |
| G28 | Incompatible shared-interface change | `verdict compat` (`actions/extra._action_compat_check`, ADR-024) exists but no CI workflow runs it | Run `verdict compat check` in CI and at E6 for changes under `contracts/`, `schemas/`, `verdict/schemas/` |
| G29 | Reviewed no-change maintenance path | Only as an override token (`pol.require-tracked-issue-ac` → `source.edit.maintenance`), which is self-issuable (B13) | Model it as a packet disposition `no-change` + reviewer id + linked current artifacts, validated by the same validator, not as an override |

## 8. Hotspots and constraints for implementers

- `hotspot: verdict/orchestration/run.py, verdict/worker_runtime.py,
  verdict/orchestration/supervisor.py`: BOD-306's gate slice (`wt/t_8a2d92f7`,
  unmerged) owns edits to the same three files. Coordinate before editing.
- Never edit `/home/nick/bin/vgate`, `.git/hooks` of the real repo,
  `verdict/openspec_vendor/`, or `docs/proof/` bundles.
- Policy rule changes alter the digest; the new digest needs operator approval
  (BOD-342 decision log), not agent self-approval (G15).
- Prime native interception (G13) cannot be solved in Verdict code. Until an
  adapter exists, report it as unsupported, never "enforced".
