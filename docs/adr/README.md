# Architecture Decision Records

Every significant architectural decision in `verdict-core` is recorded here. Each record
states the context that forced a choice, the decision itself, and the consequences the
project accepted along with it. ADRs are append-only: a decision that no longer holds is
superseded by a later record rather than edited away.

## Authority split (BOD-169 → BOD-179)

| Layer | Owns | Location |
|---|---|---|
| **Cross-repo evidence authority** | Exact SHA snapshots, duplicate hashes, conservative lifecycle + evidence levels across Verdict V2 repos | [`verdict-ecosystem` `docs/ADR_LIFECYCLE.md`](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md) + `evidence/ADR_LIFECYCLE.json` ([BOD-169](https://linear.app/bodanglin/issue/BOD-169) — **Done**) |
| **In-repo navigable index** | Reviewer-facing table for `verdict-core` alone: titles, successors, how to read shipped vs historical | **This file** ([BOD-179](https://linear.app/bodanglin/issue/BOD-179)) |

**Do not invent a competing CURRENT-heavy taxonomy.** BOD-169 deliberately marked
**0 CURRENT** because source/test path presence alone is not end-to-end runtime proof.
This index **consumes** those classifications for `verdict-core` rows and only adds:

- successor / predecessor links (including ADR-035 closing ADR-023 `MISSING_SUCCESSOR`)
- orchestration navigation (what to read first)
- banners on local duplicate copies under `docs/architecture/`

After material ADR edits land on `verdict-core` main, regenerate the ecosystem
snapshot so SHAs and digests stay aligned.

## Lifecycle vocabulary

Shared with BOD-169:

| Lifecycle | Meaning |
|---|---|
| `CURRENT` | Full executable semantic proof against an exact snapshot (BOD-169: none yet) |
| `PARTIALLY_TRUE` | Some source/test evidence exists; full decision contract not proven end-to-end |
| `SUPERSEDED` | Historical; follow the successor link |
| `MISSING_SUCCESSOR` | Declares superseded without a named successor ADR (should be closed) |
| `DUPLICATE` | Copy of a canonical record; keep for inbound-link stability |
| `STALE` | Declared status/prose no longer matches authority |
| `INVALID` | Not a product ADR (index file, fixture, template) |

Declared Status fields inside individual ADR files may still use older vocabulary
(`Accepted`, `Proposed`, …). **Lifecycle columns below follow BOD-169** unless a
newer successor ADR is named in this index.

## How to read (orchestration path)

1. [ADR-0001](0001-verdict-control-plane-invariants.md) — control-plane invariants
2. [ADR-010](ADR-010-fail-closed-capability-passports.md) — fail-closed passports
3. [ADR-032](ADR-032-core-model-metadata-store.md) — Core owns metadata; OmniRoute does not
4. [ADR-035](ADR-035-authorized-selected-route-dispatch.md) — post-swarm dispatch (supersedes 023)
5. [ADR-036](ADR-036-goal-to-receipt-orchestration.md) — goal-to-receipt orchestration (the orchestration golden path)
5. [ADR-015](ADR-015-evidence-authority-and-portable-receipts.md) — receipts
6. Ecosystem [ADR_LIFECYCLE.md](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md) — evidence bar and cross-repo duplicates

## Adding a new ADR

1. Take the next free number — check the highest existing file.
2. Name the file `ADR-0NN-short-kebab-title.md` with an `# ADR-0NN: Title` H1.
3. Include `Status`, `Date`, `Deciders`, then `Context` / `Decision` / `Consequences`.
4. Cross-reference with `Supersedes` / `Amends` / `Related`, add a row below, and
   request an ecosystem ADR lifecycle re-audit (BOD-169 process) before claiming CURRENT.

## The records (`verdict-core`)

Classifications for 0001–034 match BOD-169's audited rows. ADR-035 is new in this
hardening pass (successor for ADR-023).

| ADR | Decision | Declared status | Lifecycle (BOD-169) | Evidence | Successor / notes |
|---|---|---|---|---|---|
| [0001](0001-verdict-control-plane-invariants.md) — Control-plane invariants | Hard eligibility before ranking; concrete identity; unknown ≠ healthy | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [001](ADR-001-evidence-ledger.md) — Privacy-safe execution evidence | Versioned evidence envelope; payloads never enter the ledger | accepted | PARTIALLY_TRUE | VERIFIED | Canonical over architecture copy |
| [002](ADR-002-orchestrator-routing.md) — Thin-gate routing boundary | Deterministic gate; advisory rankers cannot restore excluded candidates | accepted | PARTIALLY_TRUE | VERIFIED | Ruflo mentions obsolete (BOD-17) |
| [003](ADR-003-platform-neutral-guidance-boundary.md) — Guidance boundary | Optional, default-off, never in enforcement path | proposed for #107 | PARTIALLY_TRUE | VERIFIED | Declared status stale vs `guidance.py` |
| [004](ADR-004-local-first-memory-plane.md) — Local-first memory plane | `MemoryPlane` + SQLite SoT | accepted | PARTIALLY_TRUE | VERIFIED | — |
| [005](ADR-005-code-intelligence-graph-memory-bridge.md) — Code graph bridge | Lightweight symbol/graph ingest into memory | Approved | PARTIALLY_TRUE | VERIFIED | — |
| [006](ADR-006-authoritative-documentation-preflight.md) — Docs preflight | Deterministic docs preflight at MemoryPlane boundary | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| [007](ADR-007-omniroute-catalog-qualification.md) — Catalog qualification | Catalog snapshots ≠ liveness | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [008](ADR-008-global-runtime-ownership.md) — Global runtime ownership | Versioned global runtime contract | proposed for #129 | PARTIALLY_TRUE | NOT_VERIFIED | Inventory still lists obsolete services |
| [009](ADR-009-durable-memory-write-gate.md) — Memory write gate | Writes through `MemoryGate` | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [010](ADR-010-fail-closed-capability-passports.md) — Capability passports | Passport required for exact route; absence denies | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [011](ADR-011-omniroute-catalog-qualification-baseline.md) — Catalog baseline | Baseline ≠ route qualification | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [012](ADR-012-consented-budgeted-probes.md) — Consented probes | Live probes need consent + budget | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [013](ADR-013-independent-protocol-surface-qualification.md) — Protocol surfaces | Chat vs Responses qualify independently | Proposed | PARTIALLY_TRUE | VERIFIED | — |
| [014](ADR-014-tool-and-structured-output-qualification.md) — Tool / structured output | Separate capability qualifications | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [015](ADR-015-evidence-authority-and-portable-receipts.md) — Portable receipts | Split portable evidence records | accepted | PARTIALLY_TRUE | VERIFIED | — |
| [016](ADR-016-deterministic-policy-and-transition-graphs.md) — Policy graphs | Hard policy compiles before ranking | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [017](ADR-017-durable-privacy-safe-receipt-ledger.md) — Receipt ledger | Local SQLite receipt boundary | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [018](ADR-018-shadow-and-counterfactual-evaluation.md) — Shadow evaluation | Versioned, payload-free evaluation artifacts | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| [019](ADR-019-runtime-negotiated-passports.md) — Runtime passports | Negotiated tool/protocol capabilities | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [020](ADR-020-gateway-adapter-contracts.md) — Gateway adapters | Provider-neutral adapter contract | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| [021](ADR-021-deterministic-provider-receipts.md) — Provider receipts | Standardized `ProviderReceipt` | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [022](ADR-022-context-provider-conformance.md) — Context conformance | Shared conformance suite | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [023](ADR-023-governed-swarm-supervision.md) — Governed swarm supervision | Ruflo/swarm deleted from Core | SUPERSEDED (BOD-17) | SUPERSEDED | VERIFIED | **Successor: [ADR-035](ADR-035-authorized-selected-route-dispatch.md)** (closes BOD-169 `MISSING_SUCCESSOR`) |
| [024](ADR-024-cross-repo-compatibility-gate.md) — Compatibility gate | Manifest + fail-closed CLI | Partially Implemented | PARTIALLY_TRUE | NOT_VERIFIED | Downstream wiring open |
| [025](ADR-025-node-envelope-enforcement.md) — Node envelope | Shared `ExecutionEnvelope` invariants | Accepted (body: proposed) | PARTIALLY_TRUE | VERIFIED | Cross-repo middleware may be incomplete |
| [026](ADR-026-responses-compatibility-boundary.md) — Responses boundary | Compatibility before HTTP Responses transport | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [027](ADR-027-observed-free-status-and-context-omissions.md) — Observed free status | Free status observed, never inferred from missing price | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [028](ADR-028-launch-gate-tooling.md) — Launch-gate tooling | Release dependency/privacy/HTTP evidence | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| [029](ADR-029-portfolio-repositioning-plan.md) — Portfolio repositioning | Hygiene / positioning / launch sequencing | Superseded | SUPERSEDED | NOT_VERIFIED | Superseded by this remediation; kept for ADR-030 cross-reference |
| [030](ADR-030-proof-carrying-decision-plane.md) — Proof-carrying decision plane | Context→decision→receipt→proof owned by Verdict | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| [031](ADR-031-prime-workflow-skills.md) — Prime workflow skills | Resume/hydrate/dispatch/proof/finish leases | Accepted for implementation | PARTIALLY_TRUE | VERIFIED | — |
| [032](ADR-032-core-model-metadata-store.md) — Core owns model metadata | OmniRoute inventory/execute/health only | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [033](ADR-033-shared-memory-provider.md) — Shared memory boundary | Shared recall advisory; local MemoryPlane authoritative | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [034](ADR-034-memory-outbox-mirror-and-fail-open-shared-recall.md) — Outbox mirror | Durable outbox; fail-open shared recall | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [035](ADR-035-authorized-selected-route-dispatch.md) — Authorized selected-route dispatch | BOD-104 serve path + BOD-67 dispatcher; demoted selectors | Accepted | PARTIALLY_TRUE | *(new — pending ecosystem re-audit)* | Supersedes ADR-023 |
| [036](ADR-036-goal-to-receipt-orchestration.md) — Goal-to-receipt orchestration | Frontier DAG, capacity-aware eligibility ladder, same-node reassignment, independent OCR review, fail-closed receipt, supervised controller | Accepted | *(new — pending ecosystem re-audit)* | VERIFIED (live runs + fresh-clone certification) | Scope successor to ADR-023 for multi-worker execution |

## Duplicate / superseded copies (retain for inbound links)

| File | Lifecycle | Canonical / successor |
|---|---|---|
| [`docs/architecture/ADR-EVIDENCE-LEDGER.md`](../archive/architecture/ADR-EVIDENCE-LEDGER.md) | DUPLICATE | [`ADR-001`](ADR-001-evidence-ledger.md) |
| [`docs/architecture/ADR-ORCHESTRATOR-ROUTING.md`](../archive/architecture/ADR-ORCHESTRATOR-ROUTING.md) | DUPLICATE | Prefer [`ADR-002`](ADR-002-orchestrator-routing.md); Ruflo framing obsolete |
| [`ADR-ORCHESTRATOR-ROUTING.md`](ADR-ORCHESTRATOR-ROUTING.md) | SUPERSEDED | BOD-17 / BOD-127 → [`ADR-035`](ADR-035-authorized-selected-route-dispatch.md) |

Byte-identical copies in `verdict-core-memory` are classified DUPLICATE by BOD-169;
`verdict-core` `docs/adr/` remains ownership.

## Explicit non-product

| File | Lifecycle | Reason |
|---|---|---|
| This `README.md` | INVALID (as an ADR record) | Index, not a decision — BOD-169 classification |
| `benchmarks/fixtures/.../ADR-001-spend.md` | INVALID | Benchmark fixture only |

## Missing decisions recorded during reconciliation

Behaviours shipped and referenced by code/docs that still warrant dedicated ADRs
or ecosystem re-proof (not silently inferred as CURRENT):

1. **Session economics STAY/SWITCH (BOD-119)** — prompt-cache value / hysteresis after hard eligibility.
2. **Effective capability + context budget (BOD-120 / BOD-125)** — model + context + tools + decomposition + verification.
3. **Full CURRENT promotion** — requires ecosystem re-audit with executable semantic proof per BOD-169 evidence bar.

## Ecosystem decision trail

| Decision | Record |
|---|---|
| Provider receipt format | [ADR-021](ADR-021-deterministic-provider-receipts.md) |
| Context provider conformance | [ADR-022](ADR-022-context-provider-conformance.md) |
| SwarmSpec governance (historical) | [ADR-023](ADR-023-governed-swarm-supervision.md) → [ADR-035](ADR-035-authorized-selected-route-dispatch.md) |
| Cross-repo compatibility gate | [ADR-024](ADR-024-cross-repo-compatibility-gate.md) |
| Node envelope enforcement | [ADR-025](ADR-025-node-envelope-enforcement.md) |
| Authoritative V2 lifecycle audit | [verdict-ecosystem ADR_LIFECYCLE](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md) (BOD-169) |
