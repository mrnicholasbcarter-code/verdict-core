# Architecture Decision Records

Every significant architectural decision in `verdict-core` is recorded here. Each record
states the context that forced a choice, the decision itself, and the consequences the
project accepted along with it. ADRs are append-only: a decision that no longer holds is
superseded by a later record rather than edited away.

## Lifecycle policy (BOD-179)

The authoritative lifecycle vocabulary for this index is:

| Lifecycle | Meaning |
|---|---|
| `CURRENT` | Decision matches shipped source/tests and is active architecture. |
| `PARTIALLY_TRUE` | Core claim holds, but parts are incomplete, cross-repo, or inventory is stale. |
| `SUPERSEDED` | Decision is historical; follow the successor link — do not treat as active. |
| `DUPLICATE` | Near-identical copy of a canonical record; keep only for inbound-link stability. |
| `STALE` | Declared status/prose no longer matches the code; needs rewrite or successor ADR. |
| `INVALID` | Not a product ADR (fixture/template) — excluded from the product index. |

Declared Status fields inside individual ADR files may still use older vocabulary
(`Accepted`, `Proposed`, `Partially Implemented`, …). **This index's Lifecycle column
is authoritative for interview/hardening readers.** When they disagree, prefer this index
and the evidence column.

## How to read this index

- Prefer rows marked `CURRENT` when learning what the product does today.
- Treat `SUPERSEDED` and `DUPLICATE` as history, not instructions.
- Check the Evidence column before assuming a behaviour is live.

## Adding a new ADR

1. Take the next free number — check the highest existing file, not the last one you
   remember.
2. Name the file `ADR-0NN-short-kebab-title.md` and open it with an `# ADR-0NN: Title` H1.
3. Include `Status`, `Date`, and `Deciders` fields, then `Context`, `Decision`, and
   `Consequences` sections.
4. Cross-reference related records with `Supersedes`, `Amends`, or `Related` fields, and add
   a row to the table below with an explicit Lifecycle classification and evidence pointer.

## The records

| ADR | Decision | Declared status | Lifecycle | Evidence / successor |
|---|---|---|---|---|
| [0001](0001-verdict-control-plane-invariants.md) — Control-plane invariants | Hard eligibility before ranking; concrete identity; unknown ≠ healthy. | Accepted | CURRENT | Aligns with `gate` / `eligibility` / `dispatcher` / `metadata` |
| [001](ADR-001-evidence-ledger.md) — Versioned, privacy-safe execution evidence | Execution evidence is a versioned, tagged envelope; payloads never enter the ledger. | accepted | CURRENT | `verdict/evidence.py`, `evidence_receipts.py`, `receipt_store.py` |
| [002](ADR-002-orchestrator-routing.md) — Thin-gate routing boundary | Verdict stays a deterministic gate; advisory rankers cannot restore excluded candidates. | accepted | PARTIALLY_TRUE | Thin-gate/`EligibilityGate` still true; Ruflo mentions obsolete (BOD-17). Prefer ADR-0001 + serve_path |
| [003](ADR-003-platform-neutral-guidance-boundary.md) — Platform-neutral guidance boundary | Guidance is optional, default-off, never in the enforcement path. | proposed for #107 | CURRENT | `verdict/guidance.py`, `tests/test_guidance.py` (declared status stale) |
| [004](ADR-004-local-first-memory-plane.md) — Local-first memory plane | Versioned `MemoryPlane` with SQLite as durable SoT. | accepted | CURRENT | `verdict/memory_plane.py`, `tests/test_memory_plane.py` |
| [005](ADR-005-code-intelligence-graph-memory-bridge.md) — Code intelligence graph bridge | Symbol/graph summaries ingested lightly into memory. | Approved | CURRENT | `verdict/code_graph.py`, `memory_bridge.py` |
| [006](ADR-006-authoritative-documentation-preflight.md) — Documentation preflight | Deterministic docs preflight at the MemoryPlane boundary. | Accepted | CURRENT | `verdict/documentation_preflight.py` |
| [007](ADR-007-omniroute-catalog-qualification.md) — OmniRoute catalog qualification | Catalog snapshots qualify as sanitized summaries, separately from liveness. | Accepted | PARTIALLY_TRUE | `omniroute_catalog.py`; refresh remains partial under baseline policy |
| [008](ADR-008-global-runtime-ownership.md) — Global runtime ownership | One versioned contract owns global runtime state. | proposed for #129 | PARTIALLY_TRUE | `runtime_contract.py` exists; service inventory still lists obsolete Ruflo/RuVector entries |
| [009](ADR-009-durable-memory-write-gate.md) — Durable memory write gate | Lifecycle/session writes pass through `MemoryGate`. | Accepted | CURRENT | `verdict/memory_gate.py`, `tests/test_memory_gate.py` |
| [010](ADR-010-fail-closed-capability-passports.md) — Fail-closed capability passports | Qualification is a versioned passport for one exact route; absence denies. | Accepted | CURRENT | `capability_passports.py`, passport eligibility tests |
| [011](ADR-011-omniroute-catalog-qualification-baseline.md) — Catalog baseline ≠ route qualification | Identity/claimed metadata baseline stays separate from route qualification. | Accepted | CURRENT | Pairs with ADR-007 / `omniroute_catalog.py` |
| [012](ADR-012-consented-budgeted-probes.md) — Consented, budgeted probes | Live probes require explicit consent and a spend budget. | Accepted | CURRENT | `probes.py`, `protocol_probes.py` |
| [013](ADR-013-independent-protocol-surface-qualification.md) — Independent protocol surfaces | Chat Completions and Responses qualify independently. | Proposed | CURRENT | `protocol_probes.py`, `tests/test_protocol_probes.py` (declared status stale) |
| [014](ADR-014-tool-and-structured-output-qualification.md) — Tool / structured-output qualification | Strict structured output and tool lifecycles qualify separately. | Accepted | CURRENT | `tool_qualification.py`, `structured_qualification.py` |
| [015](ADR-015-evidence-authority-and-portable-receipts.md) — Evidence authority + portable receipts | Route evidence splits into related portable records. | accepted | CURRENT | `evidence_receipts.py` |
| [016](ADR-016-deterministic-policy-and-transition-graphs.md) — Deterministic policy graphs | Hard-policy document compiles before ranking/execution. | Accepted | CURRENT | `policy.py`, `transitions.py` |
| [017](ADR-017-durable-privacy-safe-receipt-ledger.md) — Durable receipt ledger | Local SQLite ledger is the canonical receipt persistence boundary. | Accepted | CURRENT | `receipt_store.py`, `VERDICT_RECEIPTS_DB` |
| [018](ADR-018-shadow-and-counterfactual-evaluation.md) — Shadow / counterfactual evaluation | Evaluation artifacts are versioned and payload-free. | Accepted | CURRENT | `verdict/evaluation.py` |
| [019](ADR-019-runtime-negotiated-passports.md) — Runtime-negotiated passports | Runtime passport records negotiated tool/protocol capabilities. | Accepted | CURRENT | `runtime_passports.py` |
| [020](ADR-020-gateway-adapter-contracts.md) — Gateway adapter contracts | Provider-neutral adapter contract keeps gateway specifics out of core. | Accepted | CURRENT | `tests/test_gateway_adapters.py` |
| [021](ADR-021-deterministic-provider-receipts.md) — Deterministic provider receipts | Domain providers emit standardized `ProviderReceipt` payloads. | Accepted | CURRENT | `provider_receipts.py` |
| [022](ADR-022-context-provider-conformance.md) — Context provider conformance | Shared conformance suite pins provider behaviour across repos. | Accepted | CURRENT | `tests/test_context_provider_conformance.py` |
| [023](ADR-023-governed-swarm-supervision.md) — Governed swarm supervision | Ruflo/swarm supervision deleted from Core. | SUPERSEDED (BOD-17) | SUPERSEDED | Successor: BOD-104 execution path → BOD-67 / `dispatcher.py` |
| [024](ADR-024-cross-repo-compatibility-gate.md) — Cross-repo compatibility gate | Compatibility manifest + fail-closed gate CLI. | Partially Implemented | PARTIALLY_TRUE | Core side shipped; downstream repo wiring still open |
| [025](ADR-025-node-envelope-enforcement.md) — Node envelope enforcement | `verdict-node` enforces the same `ExecutionEnvelope` invariants. | Accepted (body: proposed) | PARTIALLY_TRUE | Core contract + TS parity tests; full node middleware may be cross-repo |
| [026](ADR-026-responses-compatibility-boundary.md) — Responses compatibility boundary | Compatibility rule applies immediately before HTTP Responses transport. | Accepted | CURRENT | `responses_compatibility.py` |
| [027](ADR-027-observed-free-status-and-context-omissions.md) — Observed free status | Free status is observed, never inferred from a missing price. | Accepted | CURRENT | `free_route_harvest.py`, free-tier admit tests |
| [028](ADR-028-launch-gate-tooling.md) — Launch-gate tooling | Release pipeline gains dependency/privacy/HTTP-surface evidence. | Accepted | CURRENT | `verdict/release/evidence.py`, launch-gate tests |
| [029](ADR-029-portfolio-repositioning-plan.md) — Portfolio repositioning plan | Hygiene, positioning, launch sequencing for the flagship story. | Accepted | PARTIALLY_TRUE | Plan ADR; BOD-17 deletion ≠ experimental relocate |
| [030](ADR-030-proof-carrying-decision-plane.md) — Proof-carrying decision plane | Verdict owns context→decision→receipt→proof; gateways are optional boundaries. | Accepted | PARTIALLY_TRUE | Large pieces shipped (`claims_ledger`, `prove_at_rest`); productization still tracked |
| [031](ADR-031-prime-workflow-skills.md) — Project-owned Prime workflow | Skills own resume/hydrate/dispatch/proof/finish with durable leases. | Accepted for implementation | PARTIALLY_TRUE | `harness_prime.py`, `bounded_recovery.py`, Prime tests |
| [032](ADR-032-core-model-metadata-store.md) — Core owns model metadata | OmniRoute is inventory/execute/health only; Core fetches models.dev + LiteLLM. | Accepted | CURRENT | `verdict/metadata/`, `tests/test_model_metadata.py` |
| [033](ADR-033-shared-memory-provider.md) — Shared memory provider boundary | Shared recall is advisory; local MemoryPlane remains authority. | Accepted | CURRENT | `shared_memory.py`, `tests/test_shared_memory_provider.py` |
| [034](ADR-034-memory-outbox-mirror-and-fail-open-shared-recall.md) — Outbox mirror + fail-open shared recall | Durable outbox mirror; shared recall fails open without poisoning local authority. | Accepted | CURRENT | `memory_outbox.py`, `memory_mirror.py` |

## Duplicate / superseded copies (retain for inbound links)

| File | Lifecycle | Canonical / successor |
|---|---|---|
| [`docs/architecture/ADR-EVIDENCE-LEDGER.md`](../architecture/ADR-EVIDENCE-LEDGER.md) | DUPLICATE | Use [`ADR-001`](ADR-001-evidence-ledger.md) |
| [`docs/architecture/ADR-ORCHESTRATOR-ROUTING.md`](../architecture/ADR-ORCHESTRATOR-ROUTING.md) | DUPLICATE | Prefer thin-gate slice in [`ADR-002`](ADR-002-orchestrator-routing.md); Ruflo framing obsolete |
| [`ADR-ORCHESTRATOR-ROUTING.md`](ADR-ORCHESTRATOR-ROUTING.md) | SUPERSEDED | Bannered SUPERSEDED (BOD-17 / BOD-127). Successor: BOD-104 → BOD-67 / `dispatcher.py` |

## Explicit non-product ADR

| File | Lifecycle | Reason |
|---|---|---|
| `benchmarks/fixtures/legit_workspace/docs/adr/ADR-001-spend.md` | INVALID | Benchmark fixture only — not product architecture |

## Missing decisions recorded during reconciliation

These behaviours are shipped and referenced by docs/code but do not yet have a dedicated ADR.
They are recorded here so they are not silently inferred:

1. **Serve-path authority (BOD-104 / BOD-127)** — `verdict/serve_path.py` + `execution_path` is the sole strategy authority on the API serve path; demoted selectors may still feed candidates but cannot invent routes when authority is required.
2. **Effective capability + context budget governors (BOD-120 / BOD-125)** — candidate sufficiency is model + context + tools + decomposition + verification, with total agent context accounted and enforced.
3. **Session economics STAY/SWITCH (BOD-119)** — prompt-cache value and route hysteresis participate in complete expected-cost comparison after hard eligibility.

## Ecosystem decision trail

| Decision | Record |
|---|---|
| Provider receipt format | [ADR-021](ADR-021-deterministic-provider-receipts.md) |
| Context provider interface standardization | [ADR-022](ADR-022-context-provider-conformance.md) |
| SwarmSpec governance model (historical) | [ADR-023](ADR-023-governed-swarm-supervision.md) — SUPERSEDED by BOD-17 |
| Verdict-ecosystem as extension, not fork | [ADR-024](ADR-024-cross-repo-compatibility-gate.md) |
| Node envelope enforcement | [ADR-025](ADR-025-node-envelope-enforcement.md) |
