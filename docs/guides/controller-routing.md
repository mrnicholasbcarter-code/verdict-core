# Controller routing (BOD-156)

Verdict selects the exact Prime root/controller provider, model, reasoning contract,
and initial context **before** a fresh autonomous session launches. The supervisor
executes and verifies that decision. It never ranks, substitutes, or falls back.

## Authority boundaries

| BOD | Role |
| --- | --- |
| BOD-104 | Final automatic execution-path authority (`optimize_execution_path`) |
| BOD-142 | Live hard eligibility before ranking |
| BOD-143 | Candidate-specific ContextPlan before final selection |
| BOD-144 | Append-only RoutingReceiptV1 evidence chain |
| BOD-119 | STAY/SWITCH continuity economics |
| BOD-149 | Worker launch authority remains separate |

No ADR is required; these boundaries do not change.

## Library seams

### `verdict/controller_launch.py`

Immutable contracts and fail-closed assembly/validation/observed-identity helpers:

- `ControllerMission` — mission/story/attempt burden and durable context refs
- `OperatorOverride` — CLI-only provider+model (+ optional reasoning) with provenance
- `PrimeLaunchTarget` — trusted binding from upstream route → Prime CLI identity
- `ObservedControllerIdentity` — owned root roster observation
- `ControllerLaunchDecision` — versioned decision with digests and receipt ref
- `PersistedAuthoritativeDecision` — already-authoritative BOD-104 decision + receipt
- `ControllerLaunchError(reason_code, detail)` — named fail-closed refusal

Public helpers:

- `decide_controller_launch(...)` — assemble/validate a decision from persisted authority
- `validate_controller_decision(...)` — freshness, digest, mission/attempt binding
- `verify_observed_controller_identity(...)` — exact provider/model/thinking match
- `observe_owned_controller_identity(...)` — exactly one owned top-level `rlmDepth=0`
- `build_prime_argv(...)` — exact approved argv (no `auto/*`, no silent downgrade)

### `verdict/controller_selection.py`

Live generation of the decision (default automatic path):

- `select_controller_launch(mission, *, hooks, override=None, session_state=None, now=None)`
- `ControllerSelectionHooks` — injectable prepare/seed/optimize/session/receipt callables
- `InjectableControllerSelector` — thin `ControllerSelector` wrapper for the supervisor

Pipeline (authorities called, not reimplemented):

1. Build seed offers from live eligible identities (injected `seed_offers`).
2. `IntelligenceService.prepare_controller_execution_request` — strict live snapshot +
   metadata, BOD-142 gates/confirmation, and BOD-143 ContextPlan per candidate.
3. Optional `decide_session_route` (BOD-119) when a durable current route exists:
   healthy → `STAY`, hard-ineligible/unhealthy → `SWITCH`, none → `BLOCKED`.
4. `optimize_execution_path` (BOD-104) over the prepared offers.
5. Bind `PrimeLaunchTarget` (exact provider/model; optional thinking only when supported).
6. `build_routing_receipt` + `persist_routing_receipt` **before** returning a launchable
   `ControllerLaunchDecision` (stores EP digest, context plan/pack/receipt digests,
   selected identity, session decision).
7. If nothing qualifies → `ControllerLaunchError(reason_code="no_eligible_route")` and
   the supervisor launches nothing.

Missing snapshot, stale evidence, digest mismatch, and `auto/*` / `default` identities
fail closed.

## Automatic vs override

**Automatic** (`--provider`/`--model` omitted together):

1. Verdict generates the decision via `select_controller_launch` from live eligibility,
   ContextPlans, BOD-104, and BOD-119 (when a durable route exists).
2. A RoutingReceiptV1 is persisted before launch.
3. Launch uses the exact approved Prime provider/model and optional supported thinking.
4. Fail closed (write BLOCKED, launch nothing) if eligibility is missing, the decision
   is stale/invalid, or no route qualifies.
5. `--controller-decision` remains an optional explicit persisted input for tests or
   operators who already authored a decision; it is **not** required for automatic mode.

**Explicit override** (both `--provider` and `--model` required):

1. One-sided flags are rejected (`incomplete_override`).
2. `auto/*`, `default`, and opaque identities are rejected.
3. Override must still be in the live eligible set; ineligible overrides fail closed.
4. CLI provenance (`source=cli`, reason, timestamp) is recorded; mission/repo/child
   text cannot forge an override.
5. An eligible override need not win economic Top-K, but hard exclusions still apply.

## Observed identity

After launch the supervisor observes `prime-agent list --json`:

- Identify exactly one owned root whose `sessionFile` is under the attempt session dir
- Require `runtimeKind=top-level` and `rlmDepth=0`
- Exact-match roster `model.provider`, `model.id`, and selected `thinkingLevel`
- Mismatch / missing / ambiguous → fail closed, stop only the owned process group,
  confirm absence, fence the attempt. Never stop unrelated sessions.
- `models.json` and argv are not observed identity. Well-formed unrelated `draft`
  roster rows are ignored.

## Continuity (BOD-119)

On fresh-session recovery, compare the durable current route with the fresh qualified
route via `decide_session_route`:

- Healthy current route → `STAY` (keep durable route)
- Hard-ineligible / unhealthy current route → `SWITCH`
- No eligible route → `BLOCKED` (launch nothing)

No mid-session model mutation. Checkpoint/handoff before fencing an old owned root.

## Operator commands

```bash
# Automatic: Verdict generates the controller decision from live authorities
python3 scripts/prime_supervisor.py --repo "$PWD" \
  --idle-seconds 600 --timeout 3600 --max-restarts 2 --max-issues 5

# Explicit override (both provider and model required; CLI provenance recorded):
python3 scripts/prime_supervisor.py --repo "$PWD" \
  --provider omniroute --model gc/grok-4.5 \
  --override-reason "operator pin for proof" \
  --idle-seconds 600 --timeout 3600 --max-restarts 2 --max-issues 5

# Optional: explicit persisted decision file (tests / pre-authored input)
python3 scripts/prime_supervisor.py --repo "$PWD" \
  --controller-decision /path/to/controller_decision.json \
  --idle-seconds 600 --timeout 3600 --max-restarts 2 --max-issues 5
```

Do not assume any example identity remains eligible. Missing live eligibility blocks
launch; there is no default-model fallback.
