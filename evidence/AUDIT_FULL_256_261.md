# Phase 1 Audit — all six issues (#256–#261) vs execution-strategy future

Audits run READ-ONLY, in parallel, citing concrete file:line + tests.

| Issue | Requirement highlight | Status | Seam needed now? |
|-------|----------------------|--------|------------------|
| #256 MODEL-001 | concrete identity, alias, qualification, passports, freshness, QUARANTINED, capacity, fail-closed | SATISFIED (alias `best/*`/`default/*` prefix forms not in opaque set = minor) | **No** — publish unchanged; future additive (add optional `surface`/`execution_strategy_id` to ModelPassport when strategy lands) |
| #257 CONTEXT-001 | provenance, freshness, source, protected-context, compression, rebind, verified/unverified, versioning | 8/9 SATISFIED | **Yes — small**: optional `authority` enum on ContextItem (context_envelope.py:135-185); otherwise schema bump 1→2 later |
| #258 CONT-001 | durable state, checkpoint, resume, replacement, replay, dup-exec protection | 5/6 SATISFIED; **side-effect/idempotency CONFLICT** | **Yes — priority**: `StepRecord.side_effect_kind` (read-only/idempotent/reversible/irreversible) + `committed` guard; else resume re-runs committed durable effects |
| #259 AUTO-001 | env discovery, research, arch, independent critic, atomic slices, bounded workers, parent review, verify, PR/CI, Ruflo-optional, no swarm-hardcode | PARTIAL/CONFLICT: hardcodes 12-stage topology as the only execution; stages 5/8/9 self-completing; no git/PR/CI stage | **Yes**: extract `ExecutionStrategyKind` (DIRECT/SWARM_AUTODEV) + `strategy_ref` on receipts; revisit critic/parent |
| #260 GOV-001 | before-model/tool/command/edit/commit/push/PR/merge/deploy; fail-closed; scoped perms; learning wall; re-validate approval | learning wall SATISFIED; durable-effect hooks ZERO but extensible (no closed registry) | **No** — publish unchanged; durable-effect auth = follow-up (optional DURABLE_EFFECTS tuple) |
| #261 CLI-001 | typed-record sim/replay; future strategy/alt/replan/env/config/capacity/verify representations | SATISFIED: replay reads schema-versioned ExecutionSession snapshot; ReceiptStore.append_event stream clean | **No** — publish unchanged; add strategy-kind contract enum later |

## Final seam decision (Phase 2 — minimal, non-breaking)

**Do (now, small):**
1. `#258` — `StepRecord.side_effect_kind` + `committed` guard (prevents re-running a committed durable effect on resume) — highest priority.
2. `#257` — optional `authority` on `ContextItem` (default unclassified; survives round-trip/digest).
3. `#259` — extract `ExecutionStrategyKind` + `strategy_ref` on `make_evidence_receipt` (de-hardcodes 12-stage as the only execution path).

**Defer (document as follow-up issues, NOT code):**
- #256 surface/entitlement seams; #260 durable-effect hooks + DURABLE_EFFECTS tuple + re-validation; #261 strategy-kind enum; full strategy compiler / pools / journal / Cockpit.

## Recommended opportunistic fixes (optional, non-blocking)
- #256: `relay.is_opaque_alias` add `best/*`/`default/*` prefix forms; add QUARANTINED-exclusion-from-selection test.
