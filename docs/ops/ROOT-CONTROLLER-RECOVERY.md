# Root controller recovery: BOD-313 phase-1 boundary audit

**Scope (2026-10-09):** This describes `integration/bod-296` at `36160f4e` and
Prime Agent 0.9.8 local docs. BOD-264 already provides **Verdict-supervised,
Verdict-launched** generations. Prime's own daemon supervisor is a *different*
process. This audit adds no recovery implementation. An operator-started
interactive root has **not** been shown to enter the Verdict supervisor.

## Existing owned-generation trace

1. `scripts/prime_supervisor.py:1918-2034` parses budgets (0-5 restarts),
   chooses the durable git-common-dir state, and creates a run ID.
   `:2307-2353` takes `supervisor.lock`, reaps stale issue leases, stops only
   its old owned daemon sessions, and admits work; multi-story mode holds a
   per-story lock through recovery (`:2376-2383`). None of these takes over
   an arbitrary interactive root.
2. Each `recover()` iteration (`:654-666`) calls `attempt()`
   (`:2063-2111`), creates a **new token and session directory**, builds
   production live selection (`:1423-1511`, `:2080-2099`), and invokes
   `resolve_controller_decision()` (`:1728-1835`). That resolver uses the
   existing `verdict/controller_selection.py:166-184` admission and
   `:849-879` selection/receipt. It fails closed without an authoritative
   decision. No selector is reimplemented here.
3. `scripts/prime_supervisor.py:2125-2140` persists `run.json` and the exact
   decision. `:2142-2202` compiles or supplies a resume instruction.
   `:1838-1863` builds `prime-agent --cwd ... --provider ... --model ...
   --session-dir <unique generation> [--thinking ...] -p --mode json <prompt>`;
   `:2215-2254` sets the active route, observes the live roster and verifies
   executed identity (`:1866-1915`) before progress monitoring.
4. `scripts/prime_supervisor.py:427-515` creates isolated one-shot Prime
   settings, launches its own process group, watches the workspace fingerprint
   (`:757-865`) and stops a stalled/deadline-exceeded attempt (`:406-424`).
   It does not watch turns in operator-owned interactive sessions.
5. `scripts/prime_supervisor.py:2255-2268` first stops the *owned* daemon
   writer (`:669-711`), then calls `record_generation_failure()`
   (`:632-651`): extracts status/log tail (`:518-575`), delegates to shared
   `verdict/orchestration/recovery.py:148-213,236-279,399-420`, writes scoped
   cooldown (`scripts/prime_supervisor.py:590-629`) into
   `~/.verdict/orchestration-health.json` (`:536-541`), and writes a
   `generation-failure-<token>.json` receipt. An account 429 is provider-scoped
   (a model-specific 429 is route-scoped); context overflow is request-scoped
   with **no** cooldown, even if text says reset; transport and timeout are
   route-scoped. Text/status heuristics require terminal evidence and cannot
   see Prime's parked-but-still-live root.
6. The next `recover()` iteration (`scripts/prime_supervisor.py:654-666`)
   reselects from fresh admission. `verdict/admission.py:226-241,1131-1150`
   reads unexpired route/provider cooldowns. The selected route is then
   verified and relaunched by steps 2-4. `scripts/prime_supervisor.py:2269-2305`
   checks a matching `outcome.json`; a deliberate BLOCKED outcome never loops.
   `tests/test_root_controller_failover.py:119-170` proves cc 429 -> kr,
   context overflow -> no cooldown, and restart bound on this owned path.

**Carry-over is external state, not an adopted runtime.** The resume prompt
reads shared `checkpoint.json` and `supervisor.json` and reconciles authority
(`scripts/prime_supervisor.py:971-1005,2175-2195`). Checkpoint validation and
monotonic progress live in `scripts/prime_state.py:236-261,291-354` (issue,
worktree/branch/SHAs, receipt/proof/PR/merge refs, blockers and next action).
Git worktrees and issue leases persist on disk (`scripts/prime_supervisor.py:769-835`;
`scripts/prime_state.py:58-151,186-230`). No code here transfers a Prime goal,
parent-scoped RLM child handles, review authority, or an integration-owner
lease. It explicitly instructs **synchronous child subprocesses** and **no
detached RLM writers** (`scripts/prime_supervisor.py:985-991,2188-2194`).
`integration.lock` is a *merge-command* flock (`scripts/prime_workflow.py:392-433`),
not a durable exclusive controller-generation lease; issue leases fence
individual PRs (`scripts/prime_state.py:205-230`). Thus the release-train
integration owner is not currently fenced across an interactive handoff.

## Prime boundary proof (local release 0.9.8 docs)

- **(a) Supported live session switch exists, but not proven externally
  targetable for an arbitrary interactive root.** `docs/quickstart.md:132-134`:
  “Use `/model` or Ctrl+L to choose a model. Use `/effort` to set the reasoning
  level.” `docs/rpc.md:259-267`: “Switch to a specific model” with
  `{"type":"set_model","provider":"anthropic","modelId":"..."}`;
  `:323-333` supplies `set_thinking_level`. `docs/extensions.md:1558-1569`
  documents `pi.setModel(model)` on the current extension session. RPC is
  **its own** stdin/stdout client (`docs/rpc.md:1-23`), not a documented
  cross-session `set_model(targetActiveSessionId)` endpoint. No documented
  CLI/daemon action applies that RPC command to a separate interactive root.
  `prime-agent --help` offers startup `--provider/--model/--thinking` and
  `attach/send/stop`, not an external model-switch command. A manual `/model`
  in the existing interactive session is safe for that session; it is not
  automatic Verdict-controlled failover. `docs/settings.md:250-257` documents
  optional `providerBackupModel` (default off), which routes *failed turns*
  to a fixed backup then returns to primary; it is not Verdict selection.
- **(b) Verdict supervisor only launches and stops sessions in its own unique
  `--session-dir`** (`scripts/prime_supervisor.py:669-711,2063-2068,2196-2202`).
  No attach/adopt input/path exists. Prime's **daemon supervisor** can adopt
  *its own* live resident workers after daemon replacement
  (`docs/daemon.md:23-38`), and `prime-agent attach` attaches a UI
  (`docs/long-running-agents.md:43-67`). Neither makes a Verdict generation
  controller of an arbitrary operator-started root. `prime-agent sessions
  --help` only offers status/usage and `--all/--json`; `prime-agent agents
  --help` opens an agents view. Prime prevents concurrent writes to one JSONL
  via session leases (`docs/daemon.md:57-66`).
- **(c) A replacement needs a compact durable checkpoint**, not the full
  transcript: objective/goal ID and source; issue/DAG and next action;
  generation, previous/new executed route, effort, source freshness and
  failure receipt; worktree/branch/HEAD, author-family evidence and reviews;
  worker session IDs/status, ownership and pending results; PR/merge
  idempotency keys, integration lease/fence, and receipts/digests. The operator's
  `~/.verdict/evidence/v2/BOD-296-CHECKPOINT.md` contains objective, current
  controller, workers/worktrees, SHAs, review state, train and next steps, but
  is an **example**, not a validated machine-readable handoff contract.
- **(d) Handles are parent-session-scoped.** `docs/rlm-runtime.md:165-173`:
  “This registry survives kernel restart, compaction, and parent restore.”
  But: “Registry scope follows the parent transcript. An unrelated new parent
  session does not inherit children.” `docs/daemon.md:27-38` says each Prime
  worker owns its root, kernel and descendants. Same-parent restore can
  rehydrate retained children; a **new root cannot adopt their RLM handles**.
  Independently addressable daemon-backed children may continue, but a new
  root must reconcile their IDs/status via supported peer messaging/observation,
  not claim inherited parent handles (`docs/rlm-runtime.md:163-173`).

## Phase-2 gaps and minimum proposed change set (sizes: rough engineering days)

| Acceptance | Gap -> proposed boundary change | Size |
|---|---|---:|
| 1, 4, 9 | Launch Verdict roots through existing supervisor by default, or prove a supported same-session `/model`/extension owner can switch the *current* interactive root with operator opt-in. If unsupported, explicit supervised handoff; refuse external RPC/CLI impersonation. | 2-4d |
| 2, 6, 9 | Feed **terminal or parked** interactive errors/stalls to existing classification/selection; add bounded alert/handoff trigger, credential-**pool** exclusion across aliases, retry/time budget/hysteresis. Existing provider cooldown alone does not prove pool independence. | 2-4d |
| 3, 7, 9 | Version and validate checkpoint schema (goal, DAG, worktrees, worker IDs/status, author families/review refs, next action); continue same transcript only if supported, otherwise reconcile peer workers without stopping unaffected ones. Verify author families from actual session logs. | 3-5d |
| 5, 9 | Add exclusive integration **controller** lease with generation ID, monotonic fence token, expiry/transfer receipt. Gate dispatch/PR/merge on token, reject stale generations, preserve idempotency keys. Keep existing merge flock and per-issue leases. | 3-5d |
| 8, 9 | Persist old/new generation, exact selected/**executed** route + model family/pool/effort, reason, freshness, checkpoint digest and fence transfer; offline fixtures for cc->cx, shared pools, parked root/retained workers, overflow, duplicate merge, unsupported switch. | 2-3d |

**Decision gate:** Do not turn on automatic interactive handoff until Prime's
session-switch ownership and a fenced integration-owner path are proven by
offline fixtures. Do not reimplement `controller_selection` or failure policy.

**Baseline:** With load1 < 6 and no matching pytest/vgate process, ran
`PYTHONPATH=/tmp/v3-313 .venv/bin/python -m pytest tests/test_prime_supervisor.py tests/test_controller_launch.py tests/test_failover_*.py -q -p no:cacheprovider`: **127 passed**, 1 pre-existing deprecation warning. Separate `tests/test_root_controller_failover.py`: **3 passed**.
