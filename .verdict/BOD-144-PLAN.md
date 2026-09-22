# BOD-144 Story Plan — RoutingReceiptV1

Base: df148f2 (main). Worktree: .worktrees/bod-144-routing-receipt
Branch: feat/bod-144-routing-receipt
Authority: Linear BOD-144 description (soft Spec). ReceiptStore remains SoT.

## Goal
One versioned durable routing receipt that explains a live Verdict decision
end-to-end: TaskProfile → evidence → candidates/exclusions → shortlist →
candidate ContextPlans → ExecutionPathDecision → final hydration →
execution identity → proof/review → outcome. Default-on via ReceiptStore.

## Non-goals / authority preserved
- No new router, context engine, or receipt DB.
- BOD-104/142/143/149/55/67 authority unchanged.
- FreeTierAdmitReceipt / ChooseReceipt / EvidenceReceipt remain contributors;
  RoutingReceiptV1 is the canonical envelope that references/digests them.

## Design (17 points)

### 1. Schema
`RoutingReceiptV1` frozen dataclass + `to_dict`/`from_dict` with sections:
identity, task_profile, evidence_snapshot, candidate_pipeline, decision,
hydration, execution, verification, final. `schema_version = "routing-receipt/v1"`.

### 2. Version / compat
- Unknown future schema → explicit `RoutingReceiptSchemaError`.
- Missing optional V1 fields parse as null/empty, never fabricated.
- Existing consumers of FreeTierAdmitReceipt/ChooseReceipt unchanged.

### 3. Reason-code registry
Central `verdict/routing_receipt.py::REASON_*` + `REASON_CODES` frozenset.
Reuse free_tier_admit codes where they map; add BOD-144 required codes:
provider_inactive, provider_unreachable, auth_failed, quota_or_capacity_denied,
required_capability_missing, required_tool_missing, context_budget_infeasible,
output_budget_infeasible, spend_policy_denied, security_policy_denied,
quality_unknown, not_shortlisted, probe_failed, stale_evidence, selected,
identity_mismatch, execution_failed, verification_failed.
Machine code + optional bounded human note. Never free-form-only.

### 4. Canonical digest
`canonical_routing_receipt(dict) -> str` via sort_keys JSON + sha256.
Digest excludes volatile presentation fields (created_at wall-clock may be
kept in payload but listed in DIGEST_EXCLUDED). Identical normalized evidence
→ identical digest. Changing candidate evidence or selected route changes digest.

### 5. Observed vs estimated
Explicit paired fields / EvidenceValue helper:
`{value, kind: estimated|observed|unknown, source, freshness}`.
Never write estimate into observed_* field. Null/unknown ≠ 0/false.

### 6. Candidate pipeline rows
Bounded `CandidateRow`: identity (gateway/provider/resource_pool/model),
eligibility, reason_codes[], task_fit{score,factors}, quality_evidence
{source,version,freshness}, capacity freshness, context_plan_digest,
confirm/passport evidence refs, reached_decision_input: bool.

### 7. Decision section
Refs ExecutionPathDecision.decision_digest; selected ConcreteRoute /
RouteIdentity fields (gateway/provider/credential_pool|resource_pool/model);
strategy/policy version; selected candidate ContextPlan digest; rationale.

### 8. Hydration section
final ContextPlan digest, ContextPack digest, role/budget allocation summary,
omissions (reason codes), prompt/input digest (not raw payload).

### 9. Execution section
requested_alias, selected_identity, observed_identity (distinct);
started/finished; status; observed usage/cost/latency when known;
provider/transport evidence refs. Mismatch → reason identity_mismatch +
fail-closed per existing serve_path authority (do not invent new policy).

### 10. Verification / final
proof/review refs + exact head/revision + result; outcome; recovery link
to newer attempt receipt_id when applicable; final decision_digest.

### 11. Persistence lifecycle (ReceiptStore)
- Root record: receipt_type="decision", payload=RoutingReceiptV1.to_dict(),
  scope=story/work_unit/attempt scope string, idempotency_key=attempt_id.
- Lifecycle: append_event for context/execution/verification/outcome.
- Partial/in-progress allowed with final.state="in_progress".
- Finalize idempotent via idempotency_key / terminal_outcome.
- Newer attempt links parent_receipt_id; never mutates prior history.
- Default path: `~/.verdict/receipts.db` or repo `.verdict/receipts.db`
  (same convention as daemon/autodev). No opt-in env var required.
- Write failure surfaces explicitly (RoutingReceiptPersistError).

### 12. Crash / restart
`load_attempt_receipt(store, scope, attempt_id)` finds partial; continue;
finalize idempotent. Fence: conflicting terminal_outcome → ReceiptConflictError.

### 13. Redaction threat model
Before put_receipt, payload already redacted by ReceiptStore.redact_sensitive_dict.
RoutingReceiptV1 builders never accept raw prompts/secrets; only digests/refs.
Tests seed canaries (api_key, bearer tokens, private blobs) and assert absence.

### 14. CLI inspection
Extend `verdict` CLI with `receipt show|list|export`:
- `verdict receipt show <receipt_id|--attempt AT>` → human + `--json`
- `verdict receipt list [--scope S]`
Uses ReceiptStore.query_receipts / get_receipt. No new DB.

### 15. Integration seam (minimal)
Builder `build_routing_receipt(...)` accepts:
FreeTierAdmitReceipt | ChooseReceipt fragments, ExecutionPathDecision,
ContextPlan/ContextPack/ContextReceipt, execution identity triple,
proof refs, attempt identity. Wire into chooser/admit finalize path via
`persist_routing_receipt(store, receipt)` helper called from a single
integration point (prefer free_tier_admit finalize / choose_route success
path OR autodev_run ledger write — keep one default-on call site + explicit
helper for tests). Do NOT fork a second ledger.

### 16. Tests (mission matrix)
New `tests/test_routing_receipt.py`:
schema round-trip, compat/refusal, digest stability, estimated≠observed,
candidate reasons, context digests, identity mismatch, crash/restart +
idempotent finalize, redaction canaries, CLI smoke.
Reuse ReceiptStore fixtures from test_durable_receipts / test_receipt_store.

### 17. Rollback
Feature is additive. If needed, leave builders unused; old admit/choose
receipts keep working. Schema gate fails closed on unsupported versions.

## Implementation order
1. `verdict/routing_receipt.py` — schema, reason codes, digest, builders, persist helpers
2. `tests/test_routing_receipt.py` — matrix
3. CLI `receipt` subcommands in `verdict/cli.py`
4. Single default-on persist hook (admit/choose or autodev ledger)
5. Local validation: pytest targeted + ruff + mypy on touched modules
6. Proof packet + independent review + PR

## Decisions / ADRs
- ADR-1: Reuse ReceiptType "decision" as root; do not extend ReceiptType enum
  unless a test proves need. Payload schema_version carries routing-receipt/v1.
- ADR-2: resource_pool maps from ConcreteRoute.credential_pool / ChooseReceipt
  resource_pool / RouteIdentity.connection as available; field name in V1 is
  `resource_pool` per Linear contract.
- ADR-3: FreeTierAdmitReceipt remains; RoutingReceiptV1 is built FROM it plus
  EP/Context/execution — not a replacement of admit receipt consumers.
