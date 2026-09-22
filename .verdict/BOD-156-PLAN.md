# BOD-156 PLAN — Verdict-selected Prime controller bootstrap

Architecture reviewed by `omniroute/cx/gpt-6-astra` on 2026-09-22.
Base: origin/main `15472818`. Worktree: `.worktrees/bod-156-controller-bootstrap`.
Branch: `feat/bod-156-controller-bootstrap`.

## Goal
Before a fresh Prime root session, Verdict must select and persist an exact,
task-bound controller contract: upstream route, Prime provider/model, supported
reasoning level (or explicit omission), and the selected ContextPlan-backed
initial prompt. The supervisor executes and verifies that decision. It never
ranks, substitutes, or falls back.

## Unchanged authority
- BOD-142: hard eligibility and task-fit shortlist.
- BOD-143: candidate-specific ContextPlan before final selection.
- BOD-104: final automatic execution-path authority.
- BOD-144: append-only RoutingReceiptV1 evidence chain.
- BOD-119: STAY/SWITCH economics and hysteresis.
- BOD-129/92: capacity/runtime certification evidence.
- BOD-149: worker launch authority remains separate.

No ADR is needed because these boundaries do not change.

## New library seam: `verdict/controller_launch.py`
Immutable contracts:
- `ControllerMission`: mission/story/attempt, objective, spend/security/tool/MCP/
  orchestration/context/proof burden and durable-context source refs.
- `OperatorOverride`: CLI-created requested Prime provider/model/reasoning plus
  source, reason, and timestamp. Mission/repo text cannot construct it.
- `PrimeLaunchTarget`: trusted binding from a BOD-104 concrete upstream route to
  Prime `provider`, `model`, optional supported `reasoning_effort`, and binding
  evidence/digest. Upstream provider/model and Prime CLI provider/model remain
  separate identities.
- `ObservedControllerIdentity`: owned root session id/file, runtime kind, RLM
  depth, Prime provider/model/thinking, and observation time.
- `ControllerLaunchDecision`: versioned decision with mission/story/attempt,
  TaskProfile/task-slice/trajectory digests, automatic|override mode, pool and
  evidence refs, ExecutionPathDecision digest, full selected route,
  PrimeLaunchTarget, selected ContextPlan/pack/receipt/prompt digests,
  constituent freshness and minimum expiry, session decision, override
  provenance, why selected, and canonical digest.
- `ControllerLaunchError(reason_code, detail)`: named fail-closed refusal.

Public functions:
```python
decide_controller_launch(mission, *, service, evidence, prime_targets,
                         override=None, session_state=None, now)
validate_controller_decision(decision, *, mission, attempt_id, now)
verify_observed_controller_identity(decision, observed)
observe_owned_controller_identity(prime, session_dir, *, timeout, poll)
```

## Trusted decision pipeline
1. Build a generic TaskProfile plus explicit controller burden contract. Do not
   use model reputation/name as evidence and do not force `frontier_required`.
2. Load production live inventory/health, Core metadata/identity map,
   passport/confirmation, policy, capability, complete-cost, and runtime
   certification evidence. Missing evidence blocks with a named reason.
3. Build evidence-backed `ExecutionPathOffer`s. Reuse
   `plan_effective_capability(...) -> AssistancePlan ->
   build_strategy_from_assistance(...) -> ExpectedStrategyCost`. Never turn
   caller JSON `eligible=true`, zero prices, or unknown certification into
   synthetic authority.
4. Expose a narrow strict wrapper around
   `IntelligenceService._prepare_execution_path_request`: in controller mode it
   requires a live snapshot and metadata, applies BOD-142 gates/confirmation,
   creates a candidate-specific BOD-143 ContextPlan for every surviving offer,
   and returns a filtered request. No missing-snapshot escape and no trusted
   prebuilt-pool escape in production controller mode.
5. Call BOD-104 `optimize_execution_path`. Preserve complete-cost, runtime
   certification, unknown-cash, and hard-exclusion floors.
6. Map the selected upstream route through a trusted `PrimeLaunchTarget`
   binding. Never strip/guess prefixes or pass upstream provider blindly to
   Prime.
7. Compile the winner's actual selected ContextPlan with
   `ContextPackCompiler.compile_units`. The resulting prompt is what launches
   Prime. Context units include durable checkpoint/mission/proof/project/tools
   provenance, not old raw chat. Account for Prime-injected AGENTS/skills/MCP
   overhead via reserve or an explicit supplied-hydration budget scope.
8. Persist RoutingReceiptV1 before launch using admit + ExecutionPathDecision +
   selected plan/pack/context receipt + selected identity and a canonical
   `controller_launch` extension. Persistence failure blocks launch.

## Explicit override
`--provider` and `--model` are a required pair. Reject one-sided, `auto/*`,
`default`, or opaque identities. Resolve the exact Prime pair through trusted
route bindings. Apply the same live hard/security/spend/tool/context/certification
checks, constrain to the exact eligible identity, and run singleton BOD-104
qualification. An eligible override need not win economic Top-K. Preserve named
hard-exclusion evidence. Record CLI provenance only; child/repo text cannot
forge it.

## Supervisor integration
- `--provider`/`--model` omitted together: automatic Verdict mode.
- both supplied: validated explicit override mode.
- optional `--thinking` only when the approved target carries exact supported
  reasoning evidence; otherwise omit and record not-selected. Never assume or
  silently downgrade.
- invoke Prime with exact approved `--provider`, `--model`, optional
  `--thinking`, unique `--session-dir`, and the selected compiled prompt.
- observe `prime-agent list --json` during bounded startup and while running.
  Identify exactly one owned root whose resolved sessionFile is under the
  attempt session dir, `runtimeKind=top-level`, `rlmDepth=0`. Bind session id and
  boundary. Verify roster `model.provider`, `model.id`, and selected
  `thinkingLevel`. Missing/ambiguous/mismatched identity fails closed, stops the
  process group, stops only owned daemon sessions, confirms absence, and fences
  the attempt. `models.json` and argv are not observed identity.
- ignore well-formed unrelated `draft` roster rows; reject malformed owned live
  identity. Existing unrelated sessions must never be stopped.
- no route/stale/invalid/missing decision or receipt persistence failure writes
  truthful BLOCKED state and launches nothing.

## Continuity
Use `decide_session_route(SessionState, qualified_route, CostState, TaskState)`
and feed its result into `ExecutionPathRequest.session_decision`. Healthy active
sessions follow actual BOD-119 STAY/SWITCH economics rather than a hard-coded
STAY. No mid-session model mutation. At recovery/expiry: persist checkpoint and
handoff, fence/stop/confirm old owned root, then obtain fresh evidence/decision
for a new attempt/session dir linked to the prior receipt. Never infer quota
exhaustion from a 429 alone.

## Receipt lifecycle
Before launch: `build_routing_receipt(... extensions={controller_launch: ...})`
then `persist_routing_receipt`. Append launch, observed-identity, compaction,
restart, failure/outcome events. Finalize with `finalize_routing_receipt`. The
append-only receipt store is authority; a mutable decision JSON is not.

## Files
- `verdict/controller_launch.py` (new — immutable contracts, decision, Prime target binding, observed-identity verification)
- `verdict/controller_selection.py` (new — BOD-142/143/104/119 selection pipeline + evidence-backed seed offers + production hooks; split from `controller_launch.py` for cohesion)
- `verdict/intelligence.py` (small strict public preparation seam only if needed)
- `scripts/prime_supervisor.py`
- `tests/test_controller_launch.py` (new)
- `tests/test_prime_supervisor.py`
- `docs/guides/prime-workflow.md`
- `docs/guides/controller-routing.md` (new operator/architecture guide)
- `.verdict/BOD-156-PLAN.md`

## Test/proof matrix
- two eligible candidates; complete-cost/task-fit winner;
- cheaper materially insufficient candidate loses;
- unhealthy/quota-exhausted excluded before ranking; UNKNOWN remains UNKNOWN;
- per-candidate ContextPlan precedes BOD-104 and selected plan compiles actual prompt;
- small/broad missions bounded; larger window does not inflate automatically;
- upstream route vs Prime target mapping remains distinct;
- exact argv and reasoning omission/support behavior;
- roster identity exact match, mismatch, missing, ambiguous, unrelated draft;
- stale/missing/invalid/no-route decision launches nothing;
- eligible/ineligible explicit override and provenance;
- receipt persisted before launch with decision/context/pack/prompt digests;
- BOD-119 STAY/SWITCH and checkpoint-before-reroute recovery;
- BOD-104/142/143/144/149 and supervisor/resume regressions;
- real automatic fresh-session proof with observed provider/model/thinking and
  pre-launch receipt on feature head and isolated clean main.

## Implementation order
1. Contract tests for evidence, target binding, freshness, override, identity.
2. Trusted offer builder + strict live preparation + BOD-104 decision.
3. Selected-plan context compilation + pre-launch BOD-144 receipt.
4. Supervisor modes, exact argv, owned-root observation/fencing/no-route.
5. BOD-119 continuity/recovery.
6. Docs and regression proof.
7. Real automatic feature-head proof; PR/CI/merge; isolated main proof.

## Rollback
Revert this change. The old supervisor still requires an explicit provider/model
but lacks the new override eligibility validation; document that as behavioral
rollback, not as equivalent safety.
