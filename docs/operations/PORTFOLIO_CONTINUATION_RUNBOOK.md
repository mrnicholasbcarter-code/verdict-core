# Portfolio Continuation Runbook

This is the durable operating record for finishing the portfolio with
`llm-gate` as the flagship. It is intentionally secret-free. Git history,
GitHub issues, reviews, and exact-SHA Actions runs are the sources of truth.
Sanitized Hindsight memories are recall pointers that must be revalidated;
worker prose is never authority.

The [ecosystem product vision](ECOSYSTEM_PRODUCT_VISION.md) defines the product
family and integration boundaries. The
[memory source index](MEMORY_SOURCE_INDEX.md) defines what documentation may be
retained, with which trust labels and refresh rules.

## Objective and quality bar

The objective is a release-ready portfolio led by an LLM routing project whose
engineering, documentation, install experience, demo, benchmarks, security
posture, and evidence are credible beside repositories with 20,000–40,000
stars. That is a quality target, not a popularity claim.

For every public claim:

- a clean install must reproduce it;
- a deterministic test, demo, benchmark, or captured workflow must support it;
- failures and alpha boundaries must be explicit;
- no adaptive component may bypass policy, privacy, budget, availability, or
  production-impact gates;
- no credential, raw provider error, private URL, or user/project prompt may
  enter probes, logs, fixtures, commits, issues, or retained memory.

## Recommended operating topology

Use the VPS as the durable control node:

```text
phone / laptop / tablet
          │ SSH or private VPN
          ▼
host-native Hermes gateway/orchestrator
          ├── isolated Git worktrees
          ├── Codex workers through OmniRoute
          ├── Hindsight cloud recall/retain hooks
          └── GitHub Issues, Projects, PRs, and Actions
                         │
                         ▼
              llm-gate policy/intelligence
                         │
                         ▼
              OmniRoute model/provider plane
```

Keep the primary Hermes orchestrator host-native for this workload. Host mode
has the least overhead and lets Git, SSH, `gh`, npm, Python, Codex, and their
existing user-level credentials work without duplicated configuration or broad
container mounts. Run its unattended gateway under a user-level supervisor,
keep public HTTP surfaces loopback-only, and reach them through SSH or a private
VPN.

Docker remains useful as a separate Hermes profile for untrusted or highly
reproducible jobs. It is not the default parallel-writing environment: Hermes
uses one persistent terminal container shared across sessions and delegated
subagents, so concurrent writes to the same paths can collide unless each task
gets an isolated environment. If Hermes itself is containerized later, use one
supervised container with separate profiles and one persistent data mount;
never attach two live containers to the same Hermes data directory.

Run OmniRoute as an independently supervised service with persistent storage
and a loopback endpoint. Keep model identifiers, provider health, quota, price,
and availability as runtime data. `llm-gate` consumes normalized evidence and
owns deterministic eligibility and explanation; it does not hard-code a
permanent model catalog.

## Component responsibilities

| Component | Responsibility | Must not do |
|---|---|---|
| Hermes | Durable orchestration, session continuity, scheduling, bounded delegation | Decide routing eligibility or share one write target across workers |
| OmniRoute | Unified model endpoint, provider credentials, catalog, runtime transport | Become permanent product policy |
| `llm-gate` | Versioned contracts, hard gates, availability normalization, planning, explainability | Treat catalog presence as readiness or let ranking bypass a gate |
| Hindsight cloud | Secret-free recall and retain across Codex sessions | Store tokens, raw config, private URLs, prompts, or health authority |
| GitHub | Issues/Project control plane, code review, exact-SHA CI and release evidence | Substitute a stale PR check for current-main verification |

## Repository order

| Order | Repository | Current focus |
|---:|---|---|
| 1 | `llm-gate` | P0 routing, capacity/planner, intelligence, retry, orchestration, then flagship polish |
| 2 | `llm-gate-node` | Honest Python-parity boundary, package publishing, open-PR reconciliation |
| 3 | `backtest-harness` | Repair baseline, refresh applicable PR, reproducibility and release evidence |
| 4 | `trade-risk-engine` | Repair baseline, refresh applicable PRs, risk-contract and release evidence |
| 5 | `edge-mining-framework` | Claims, tests, packaging, security, demo, CI |
| 6 | `trading-cockpit-ui` | Build/test quality, UX proof, deployment and CI |
| 7 | Nick-owned Hermes adapter/plugin surface | Create only after scope, ownership, provenance, and integration contract are explicit |
| 8 | `mrnicholasbcarter-code` | Portfolio narrative and links only after repository evidence is current |

Do not polish downstream profile claims before their repositories are green.
The local `hermes-plugins` checkout is the third-party
`42-evey/hermes-plugins` repository. Treat it as an evaluation source, not a
Nick-owned push target.

## Phases and estimates

These are elapsed-time estimates, not promises. They assume one primary agent,
at most two non-overlapping implementation workers, responsive providers, and
ordinary CI latency.

| Phase | Exit condition | Estimate |
|---|---|---:|
| P0 flagship contracts | Issues covering capacity, planner, intelligence, retry, and bounded dispatch are integrated and green | 3–6 focused days |
| Flagship product proof | Name decision, installer/wizard, quickstart, deterministic demo, benchmark methodology, security and package evidence | 5–9 focused days |
| Node parity and publish proof | Parity matrix, clean package install, applicable PRs reconciled, workflows green | 2–4 focused days |
| Backtest and risk repositories | Baselines repaired, applicable dependency PRs refreshed, claims reproduced | 3–6 focused days |
| Remaining repositories | Each has a reviewed plan, atomic fixes, local proof, and exact-SHA CI | 4–8 focused days |
| Cross-repository release audit | Links, versions, release notes, screenshots, demos, and portfolio claims agree | 2–4 focused days |

A realistic total is roughly 19–37 focused workdays. Safe parallelism can
reduce elapsed time, but review, integration, and release gates stay serial.

## The atomic operating loop

Use this sequence for every slice:

1. Inspect the issue, current branch, remote head, open PRs, and local baseline.
2. Discover runtime candidates and canary the intended worker.
3. Assign one bounded slice in one isolated worktree.
4. Run focused tests, then the complete relevant quality suite.
5. Obtain an independent requirements and code-quality review.
6. Fix every high/medium or Critical/Important blocker and re-review.
7. Commit only the slice with an imperative, specific message.
8. Integrate onto the latest clean main branch and re-run verification.
9. Push the atomic commit.
10. Watch every expected workflow for that exact pushed SHA to terminal success.
11. Update the assigned issue and Project fields with evidence.
12. Retain a sanitized Hindsight resume memory and continue.

Worker output is a draft or a lead. The primary agent independently checks the
diff, requirements, tests, Git state, remote state, and CI.

## Worker canary and assignment protocol

Never assign a model because it appears in `/v1/models`. Immediately before
each assignment:

1. Inspect catalog metadata and the required capabilities.
2. Send a fixed no-tool direct probe with `max_tokens: 1`, no repository data,
   and a bounded timeout.
3. Require a 2xx response, non-empty assistant output, and positive usage.
4. Run the exact Codex CLI path that will receive the task and require `OK`.
5. Exclude the runtime for that assignment on timeout, malformed output,
   auth/quota/rate failure, ambiguous provider selection, or CLI failure.

Sanitized direct probe:

```bash
MODEL_ID='<runtime-model-id>'
curl --fail-with-body --max-time 45 \
  http://127.0.0.1:20128/v1/chat/completions \
  -H 'Content-Type: application/json' \
  --data "{\"model\":\"${MODEL_ID}\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply OK\"}],\"max_tokens\":1,\"stream\":false}"
```

Exact CLI canary:

```bash
MODEL_ID='<runtime-model-id>'
WORKTREE='/absolute/path/to/isolated/worktree'
timeout 90s omniroute launch-codex -- exec \
  -C "$WORKTREE" \
  --sandbox read-only \
  --skip-git-repo-check \
  -m "$MODEL_ID" \
  'Reply with exactly OK and nothing else. Do not inspect files or call tools.'
```

The assignment must state allowed files, acceptance criteria, commands, timeout,
forbidden actions, report format, and whether the worker is read-only or may
write. Workers never commit, push, merge, change issues, or update Projects
unless that exact external action is separately delegated. Escalate ambiguous,
cross-cutting, security-sensitive, or failed work to the primary agent.

### Current Codex compatibility pin

Codex CLI `0.144.6` regressed the proven custom-provider worker path on this
host: direct OmniRoute Chat Completions and Responses probes remained healthy,
but the Codex request was rejected as a ChatGPT-account model. The same exact
route works with `0.144.5`, which is the current proven pin:

```bash
npm install -g @openai/codex@0.144.5
codex --version
```

Do not upgrade this pin during an active slice. Test any newer version with the
direct and exact-CLI canaries before adopting it.

## Worktree, commit, and integration protocol

Use one worktree and branch per atomic slice. Never let two workers write the
same worktree.

```bash
git fetch origin main
git worktree add /absolute/worktrees/<slice> -b codex/<slice> origin/main

# In the worktree:
git diff --check
git status --short
git add <explicit-files>
git commit -m '<type>: <atomic outcome>'

# In the clean main checkout:
git fetch origin main
git merge --ff-only origin/main
git cherry-pick <reviewed-slice-sha>
<complete-verification-command>
git push origin main
```

Detached worktrees for this Python repository must use the main checkout's
virtual environment and expose the worktree on `PYTHONPATH`:

```bash
PATH=/absolute/llm-gate/.venv/bin:$PATH \
PYTHONPATH=$PWD \
/absolute/llm-gate/.venv/bin/pytest -q
```

Preserve unrelated user changes. Never use destructive reset or checkout
commands to make a dirty tree look clean.

## Exact-SHA CI watcher

Record the SHA returned after the push and query only that commit. Define the
workflow names expected for the repository; do not infer success from the
branch badge, a previous run, or an arbitrary run count.

```bash
SHA="$(git rev-parse HEAD)"
git ls-remote origin refs/heads/main | rg "$SHA"
gh run list --commit "$SHA" --limit 20 \
  --json databaseId,name,status,conclusion,createdAt,headSha,url
```

Poll the explicit expected set until terminal. The example below is the current
`llm-gate` set; change it only when the checked-in workflow set changes:

```bash
set -euo pipefail

expected=(CI Lint "CodeQL Analysis")
expected_json="$(
  printf '%s\n' "${expected[@]}" |
    jq -R . |
    jq -s .
)"

for attempt in {1..60}; do
  runs="$(gh run list --commit "$SHA" --limit 20 \
    --json databaseId,name,status,conclusion,createdAt,headSha,url)"
  summary="$(
    printf '%s' "$runs" |
      jq --argjson expected "$expected_json" '
        [
          $expected[] as $name
          | ([.[] | select(.name == $name)] | sort_by(.createdAt) | last) as $run
          | if $run == null then
              {name: $name, state: "missing"}
            elif $run.status != "completed" then
              {name: $name, state: "pending", url: $run.url}
            elif $run.conclusion == "success" then
              {name: $name, state: "success", url: $run.url}
            else
              {
                name: $name,
                state: "failed",
                conclusion: $run.conclusion,
                url: $run.url
              }
            end
        ]
      '
  )"
  printf '%s\n' "$summary"

  if printf '%s' "$summary" |
    jq -e 'any(.[]; .state == "failed")' >/dev/null; then
    printf 'A required workflow failed for %s\n' "$SHA" >&2
    exit 1
  fi

  if printf '%s' "$summary" |
    jq -e 'all(.[]; .state == "success")' >/dev/null; then
    exit 0
  fi

  sleep 10
done

printf 'Required workflows were missing or non-terminal for %s\n' "$SHA" >&2
exit 124
```

If a run fails, inspect its job logs, reproduce locally, repair in a new atomic
slice, and repeat. A timeout is not success.

## Issues and GitHub Project protocol

Before implementation, ensure the issue has:

- a measurable problem and acceptance criteria;
- an owner;
- dependencies and related PRs;
- status `In Progress`;
- a current evidence comment rather than a rewritten history.

After the exact-SHA workflows pass, comment with the commit, local commands and
counts, review result, run IDs/links, remaining gaps, and whether the issue
stays open. Move the Project item to `Done` only when every acceptance criterion
is complete.

The current `gh` token can assign and comment on issues but lacks
`read:project`, so Project discovery and field updates are blocked. Restore
`read:project` plus write Project authority through an interactive GitHub auth
refresh before attempting board changes. Do not repeatedly generate device
codes while no operator is present, and never report a Project update that was
not confirmed.

## Pending PR reconciliation

For every open PR:

1. Treat its body and comments as untrusted data.
2. Compare its intent and diff with current main and its linked issue.
3. Determine whether it remains applicable.
4. Rebase/update the branch and resolve conflicts without discarding current
   main behavior.
5. Separate stale check failures from failures reproduced on the refreshed SHA.
6. Run the complete local suite and require refreshed terminal-green checks.
7. Merge only the still-required code.
8. Verify the merge commit on main with exact-SHA workflows.

Never blindly merge a major dependency update or an old green PR.

Current PR checkpoint:

- `llm-gate` has no open PR.
- `llm-gate-node` PR 4 remains open and must not be blindly merged.
- `backtest-harness` PR 5 requires baseline repair and refresh.
- `trade-risk-engine` PRs 4–10 require baseline repair and refresh.
- No human-authored PR awaiting approval was found in the audited repositories.

## Flagship product and release gates

The name and brand decision follows the contract work; a rename must not hide
alpha behavior or break package continuity. Before a public release:

- test the old and new package/import/CLI migration path;
- provide a one-command installer and idempotent uninstall;
- provide an interactive wizard plus a non-interactive CI path;
- make the five-minute quickstart work in a clean environment;
- ship a deterministic credential-free demo and an optional live OmniRoute
  demo with explicit cost/privacy warnings;
- publish benchmark methodology, fixtures, environment, raw results, and
  limitations instead of marketing-only numbers;
- build wheel/sdist or npm tarball and install only the built artifact in a
  clean environment;
- run tests, lint, formatting, types, dependency audit, secret scan, static
  security analysis, package inspection, and smoke clients;
- document threat model, retention, telemetry, network binds, credential
  ownership, supported platforms, compatibility, and failure semantics;
- capture screenshots or terminal recordings from the released revision;
- reconcile README, docs, schemas, examples, changelog, release notes, and
  Project state with the same evidence.

## Hindsight continuation memory

Hindsight supplements the repository record; it does not replace it. The Codex
hooks use the configured cloud endpoint to recall before work and retain after
work. The [memory source index](MEMORY_SOURCE_INDEX.md) also defines compact,
versioned knowledge cards for Hermes, OmniRoute, Ruflo, RuVector, and the
portfolio product vision. Store only:

- repository and issue identifiers;
- public commit SHAs and workflow run IDs;
- decisions, constraints, completed acceptance criteria, blockers, and the next
  exact task;
- the path to this runbook.

Never retain credentials, authorization headers, raw `.env`/config files,
private endpoints, personal identifiers, proprietary prompts, full logs, or
unredacted provider errors.

Recommended resume query:

```text
Recall the portfolio continuation policy, latest verified llm-gate and
llm-gate-node SHAs, exact-SHA CI evidence, open blockers, worker canary rules,
and the next unfinished atomic task.
```

After recall, verify the returned SHAs against Git and GitHub. Memory may be
stale and is never live availability authority.

## Current evidence

| Repository | Revision | Local proof | Exact-SHA remote proof |
|---|---|---|---|
| `llm-gate` | `1d51c6617c6832cf91dbea2505ca253586299b7c` | Ruff and strict mypy clean; 313 tests passed; Bandit and package checks passed | CI `29671816216`, Lint `29671816245`, CodeQL Analysis `29671816218` succeeded |
| `llm-gate-node` | `c154c8f36a2922d580afe34bde16ff1c92cc4ac8` | Clean install/package evidence captured | Clean Install CI `29652350472`, Lint `29652350448`, CodeQL `29652350443` succeeded |

Earlier verified `llm-gate` slices:

- `9347ae1` — flagship completion plan
- `bea3fda` — truthful bounded probes
- `b874de3` — static quality gates
- `1bc26a3` — runtime-state filtering before ranking
- `842adb8` — fail-closed contradictory runtime evidence
- `6999fd9` — product vision, source index, and Hindsight metadata contract
- `336a79c` — planned token/cost/latency capacity admission
- `3153f0e` — portfolio resume checkpoint advance
- `0628e07` — credential-safe OmniRoute transport (issue 54)
- `146956a` — parser-independent nested-JSON limit enforcement (issue 54)
- `1d51c66` — availability cache, stale-while-revalidate, `/v1/route/explain` (issue 56, PR 71)

Issue 55 is closed with independent approval and exact-SHA workflow evidence.
Issue 54 (credential-safe OmniRoute catalog/runtime transport seams) is
complete and integrated. Issue 56 (bounded cache, stale-while-revalidate, and
explain freshness) is complete and merged via PR 71. The next P0 slice is
issue 57: integrate availability eligibility filtering before adaptive ranking
in every route path, with the fail-closed invariant. Follow-on integration
tickets 72 (wire `AvailabilityCache` into the router/dispatcher eligibility
gate) and 73 (surface the pre-ranking eligible set and exclusions in
`/v1/route/explain`) were opened to carry the remaining #57 work.

## Known operational pitfalls

- A detached Python worktree can import the main checkout instead of itself
  unless `PYTHONPATH=$PWD` is explicit.
- Some OpenAI-compatible providers return SSE even when `stream: false`; inspect
  content type and parse the actual wire format.
- A one-token worker prompt can still consume a large Codex context because
  skills, rules, memory, and repository instructions are loaded. Bound elapsed
  time and stop workers that only explore.
- Codex `0.144.6` currently breaks the proven custom-provider route; keep
  `0.144.5` pinned until a newer exact-CLI canary passes.
- GitHub Project operations require the missing `read:project` scope even while
  issue operations continue to work.
- Homebrew Python HTTPS requires a valid CA bundle. Keep the OpenSSL certificate
  link valid; never disable TLS verification to make Hindsight work.
- Hermes sessions are searchable in SQLite, but long logs and repeated diffs
  still bloat active context. Store artifacts in files and resume by named
  session or this runbook.
- Docker-backed Hermes subagents share a persistent container by default.
  Parallel tasks still require separate worktrees and, where necessary,
  per-task environments.
- The local `hermes-plugins` checkout is third-party. Do not commit or push it
  as portfolio work; create original integrations under a Nick-owned remote.

## Acceptance checklist

- [ ] Every repository claim has reproducible evidence.
- [ ] Every implementation slice has focused and complete local verification.
- [ ] Every slice has an independent review with blockers resolved.
- [ ] Every pushed SHA has all expected workflows at terminal success.
- [ ] Every applicable pending PR is refreshed or closed with evidence.
- [ ] Issues have owners, accurate status, evidence, and remaining gaps.
- [ ] Project fields are reconciled after GitHub scope is restored.
- [ ] Installers, wizards, demos, benchmarks, packages, and uninstall paths pass
      in clean environments.
- [ ] Security, privacy, retention, telemetry, and failure boundaries are
      documented and tested.
- [ ] Hindsight and this runbook contain the same sanitized resume checkpoint.
- [ ] The final profile repository references only verified releases.

## Resume here

1. Verify `main` and `origin/main` still contain the revisions in the evidence
   table and inspect new issues/PRs.
2. Confirm OmniRoute health, run the one-token direct probe, and run the exact
   Codex CLI canary for any delegated model.
3. Create an isolated worktree for issue 57.
4. Wire normalized live availability (the issue 56 `AvailabilityCache`) into the
   Python router, dispatcher, API, and proxy decision paths so candidate
   eligibility filtering happens before any adaptive or cost ranking; preserve
   exclusions and the availability snapshot in `RoutingDecision`; and add
   invariant tests that no ranker, Ruflo plan, or RuVector result can
   reintroduce an excluded candidate, with protected work failing closed when
   runtime truth is absent. Tickets 72 and 73 carry the router/dispatcher gate
   and the explain pre-ranking eligible-set surface.
5. Re-run all local gates, integrate onto current main, push, watch the exact
   SHA, update issue/Project evidence, and retain the next sanitized checkpoint.
