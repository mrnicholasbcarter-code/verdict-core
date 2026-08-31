# Capability Discovery: Operational Routing Loop

**Feature:** `specs/272-operational-routing-loop`
**Decision:** EXTEND existing Verdict contracts and adapters; do not build a parallel router.

## Existing capabilities

| Need | Existing capability | Decision |
|---|---|---|
| Task intake and effort estimation | `verdict/planner.py:StructuredPlanner` | REUSE |
| Catalog/runtime normalization | `verdict/availability.py:OmniRouteAvailabilityAdapter` and `verdict/omniroute.py` | EXTEND with evidence completeness only where required |
| Hard pre-ranking admission | `verdict/eligibility.py:EligibilityGate` and `verdict/policy.py:Policy` | REUSE |
| Provider-neutral gateway declarations | `verdict/gateway_adapters.py` | REUSE the versioned manifest, route identity, request, capability negotiation, and failure vocabulary |
| One live repository-edit path | `verdict/autodev_run.py`, `verdict/patch_executor.py`, `verdict/work_unit.py`, and the existing `verdict autodev` CLI | EXTEND this path; do not create a parallel operational runner |
| Portable decision/outcome contracts | `verdict/contracts.py` | EXTEND with loop/checkpoint references only if absent |
| Exact source-state binding | `verdict/trusted_change_report.py:capture_source_state` and `verdict/contracts.py:SourceState` | REUSE |
| Durable step/checkpoint/failure state | `verdict/execution_session.py:ExecutionSession` | EXTEND only for packet-bound immutable inputs and resume metadata |
| Bounded, provenance-bearing context | `verdict/context_pack.py:ContextPackCompiler` | REUSE a minimal deterministic subset for US1 |
| Privacy-safe append-only receipts | `verdict/receipt_store.py` and `verdict/evidence.py` | REUSE |
| Protocol/tool qualification | `verdict/protocol_probes.py` | REUSE |
| Verification before promotion | `verdict/autodev_run.py:_verify`, existing trusted-change evidence, and verifier contracts | REUSE the single-work-unit path; swarm verification is later-phase only |
| Advisory intelligence | `verdict/intelligence.py` | REUSE; it cannot bypass eligibility |

## Relevant decisions

- ADR-002 keeps orchestration/model selection separate from Verdict's deterministic safety floor.
- ADR-010 and ADR-015 make missing, stale, malformed, or contradictory hard evidence fail closed.
- ADR-016 requires deterministic policy transitions and bounded retries.
- ADR-017 requires privacy-safe durable receipts and scoped retention.
- ADR-023 requires bounded swarm envelopes and verifiable worker outcomes.
- ADR-026 preserves the Responses/Chat compatibility boundary; protocol success is independently qualified.

## Rejected alternatives

- **BUILD a new routing engine:** duplicates planner, availability, eligibility, dispatcher, and receipt authority.
- **REUSE OmniRoute task routing:** prohibited by the handoff; task-routing and detection remain disabled.
- **ADOPT private Ruflo/RuVector storage:** violates the public adapter and evidence boundary; only documented external interfaces may be used.
- **Treat catalog rows as ready:** rejected because catalog presence is not live reachability, quota, capability, or protocol proof.

## Runtime constraints

- Live OmniRoute remains 3.8.49; the 3.8.50 source snapshot is reference material only.
- `taskRouting.enabled=false` and `detectionEnabled=false` remain required.
- Current live health shows provider/circuit information, but the quota command reports `No quota data`; absent headroom remains unknown.
- The live model catalog proves catalog presence only. A route must still be qualified for the exact protocol and work unit before execution.
- Baseline evidence is 1,449 passing tests from the handoff; implementation must preserve it.
- No provider credentials, private databases, opaque `auto/*` routes, publication, or merge are in scope.

## First slice

The first product slice extends the existing `verdict autodev` path for exactly one
bounded work unit. It binds source/scope/verification in a durable packet, compiles
a small deterministic context pack, discovers and qualifies a concrete non-primary
route, applies Verdict eligibility, asks the live model for a patch, enforces owned
paths before apply, verifies outside the model, and emits resumable receipts. A
dry-run may preview this path but cannot satisfy Phase 1 delivery. Decomposition,
teams, swarms, semantic/GraphRAG retrieval, learning, and multi-gateway parity remain
promotion stories.

## Proof-level inventory for the first slice

| Surface | Proof level | Exact reuse seam | Missing Phase 1 seam |
|---|---|---|---|
| `capture_source_state` | `PARTIAL` | Reads commit, branch, dirty and untracked path lists into `SourceState`. | It does not digest dirty/untracked contents, lockfiles, scope, acceptance, or policy; the packet must bind those without changing Trusted Change Report semantics. |
| `ExecutionSession` | `SOURCE-ONLY` | Persists ordered steps, checkpoints, artifacts, failures, attempts, model substitution, and committed-side-effect guards through `MemoryPlane`. | No packet/source/policy/context immutable-digest validation or packet receipt vocabulary exists. |
| `ContextPackCompiler` | `SOURCE-ONLY` | Enforces candidate-specific token budgets, required slots, provenance decisions, sanitization, receipts, and deterministic packing. | `autodev` does not currently construct the Phase 1 source/test/instructions/docs units or render the pack into its patch prompt. |
| `gateway_adapters.py` and runtime contracts | `FIXTURE-ONLY` | Provider-neutral capability negotiation, route identity, request translation, attestation, telemetry allowlist, and normalized failures have conformance fixtures. | No live OpenAI-compatible/OmniRoute adapter connects these contracts to `autodev`; actual served identity is not captured there. |
| `EligibilityGate` | `SOURCE-ONLY` | Single pre-ranking filter with protected fail-closed behavior and preserved exclusion explanations. | `run_autodev` currently accepts an executor model string directly and never calls the gate. |
| `autodev_run.py` | `PARTIAL` | Existing unit path gets a model patch, enforces owned paths, applies it, runs an external argv, attributes changed files, records usage/latency, and writes receipts. Unit tests exercise the path with injected transports. | Live qualification, packet/source validation, deterministic context compilation, clean attempt isolation, actual-route attestation, restart-safe checkpoints, and one bounded fallback are absent. |
| `PatchExecutor` | `PARTIAL` | Calls an OpenAI-compatible chat endpoint, parses a unified diff, rejects out-of-bound paths before apply, applies with `git apply`, and returns reported usage. | It records the requested model only; it does not expose gateway response headers/metadata needed for actual route attribution or normalized retry safety. |
| `ReceiptStore` | `SOURCE-ONLY` | Append-only scoped SQLite receipts, redaction, integrity verification, replay, export/import, retention, and legal holds. | No execution-packet/transition receipt type is wired into the product path yet. |
| Live OmniRoute 3.8.49 | `LIVE-PROVEN` for health/catalog/settings/telemetry only | Read-only CLI observations prove a healthy runtime, concrete catalog projection, provider/circuit summary, and aggregate telemetry at the recorded time. | Exact task-route protocol suitability is `PARTIAL`; quota/headroom is `UNKNOWN`; live patch execution remains `MISSING` until T019. |

`LIVE-PROVEN` is deliberately narrow: an observed management surface does not
prove that any listed model can complete the frozen work unit.

## Gate result

The repository has no active `.specify/` project scaffolding or constitution file, so the standard Spec Kit prerequisite script cannot be run here. The feature artifacts are therefore written directly under the handoff's canonical `specs/272-operational-routing-loop/` directory and are validated with repository-local checks below.
