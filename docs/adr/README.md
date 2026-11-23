# Architecture Decision Records

Every significant architectural decision in `verdict-core` is recorded here. Each record
states the context that forced a choice, the decision itself, and the consequences the
project accepted along with it. ADRs are append-only: a decision that no longer holds is
superseded by a later record rather than edited away.

## Authority split (the cross-repo ADR lifecycle evidence audit → the in-repo ADR navigable index)

| Layer | Owns | Location |
|---|---|---|
| **Cross-repo evidence authority** | Exact SHA snapshots, duplicate hashes, conservative lifecycle + evidence levels across Verdict V2 repos | [`verdict-ecosystem` `docs/ADR_LIFECYCLE.md`](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md) + `evidence/ADR_LIFECYCLE.json` (the cross-repo ADR lifecycle evidence audit — **Done**) |
| **In-repo navigable index** | Reviewer-facing table for `verdict-core` alone: titles, successors, how to read shipped vs historical | **This file** |

**Do not invent a competing CURRENT-heavy taxonomy.** The cross-repo ADR lifecycle evidence audit deliberately marked
**0 CURRENT** because source/test path presence alone is not end-to-end runtime proof.
This index **consumes** those classifications for `verdict-core` rows and only adds:

- successor / predecessor links (including ADR-035 closing ADR-023 `MISSING_SUCCESSOR`)
- orchestration navigation (what to read first)
- banners on local duplicate copies under `docs/architecture/`

After material ADR edits land on `verdict-core` main, regenerate the ecosystem
snapshot so SHAs and digests stay aligned.

## Lifecycle vocabulary

Shared with the cross-repo ADR lifecycle evidence audit:

| Lifecycle | Meaning |
|---|---|
| `CURRENT` | Full executable semantic proof against an exact snapshot (the cross-repo ADR lifecycle evidence audit: none yet) |
| `PARTIALLY_TRUE` | Some source/test evidence exists; full decision contract not proven end-to-end |
| `SUPERSEDED` | Historical; follow the successor link |
| `MISSING_SUCCESSOR` | Declares superseded without a named successor ADR (should be closed) |
| `DUPLICATE` | Copy of a canonical record; keep for inbound-link stability |
| `STALE` | Declared status/prose no longer matches authority |
| `INVALID` | Not a product ADR (index file, fixture, template) |

Declared Status fields inside individual ADR files may still use older vocabulary
(`Accepted`, `Proposed`, …). **Lifecycle columns below follow the cross-repo ADR lifecycle evidence audit** unless a
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
   request an ecosystem ADR lifecycle re-audit (the cross-repo ADR lifecycle evidence audit process) before claiming CURRENT.

## The records (`verdict-core`)

Classifications for 0001–034 match the cross-repo ADR lifecycle evidence audit's audited rows. ADR-035 is new in this
hardening pass (successor for ADR-023).

| ADR | Decision | Declared status | Lifecycle | Evidence | Successor / notes |
|---|---|---|---|---|---|
| [0001](0001-verdict-control-plane-invariants.md) — Control-plane invariants | Hard eligibility before ranking; concrete identity; unknown ≠ healthy | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [001](ADR-001-evidence-ledger.md) — Privacy-safe execution evidence | Versioned evidence envelope; payloads never enter the ledger | accepted | PARTIALLY_TRUE | VERIFIED | Canonical over architecture copy |
| [002](ADR-002-orchestrator-routing.md) — Thin-gate routing boundary | Deterministic gate; advisory rankers cannot restore excluded candidates | accepted | PARTIALLY_TRUE | VERIFIED | Ruflo mentions obsolete |
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
| [023](ADR-023-governed-swarm-supervision.md) — Governed swarm supervision | Ruflo/swarm deleted from Core | SUPERSEDED | SUPERSEDED | VERIFIED | **Successor: [ADR-035](ADR-035-authorized-selected-route-dispatch.md)** (closes the cross-repo ADR lifecycle evidence audit `MISSING_SUCCESSOR`) |
| [024](ADR-024-cross-repo-compatibility-gate.md) — Compatibility gate | Manifest + fail-closed CLI | Partially Implemented | PARTIALLY_TRUE | NOT_VERIFIED | Downstream wiring open |
| [025](ADR-025-node-envelope-enforcement.md) — Node envelope | Shared `ExecutionEnvelope` invariants | Accepted (body: proposed) | PARTIALLY_TRUE | VERIFIED | Cross-repo middleware may be incomplete |
| [026](ADR-026-responses-compatibility-boundary.md) — Responses boundary | Compatibility before HTTP Responses transport | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [027](ADR-027-observed-free-status-and-context-omissions.md) — Observed free status | Free status observed, never inferred from missing price | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [028](ADR-028-launch-gate-tooling.md) — Launch-gate tooling | Release dependency/privacy/HTTP evidence | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| 029 — Portfolio repositioning (removed, BOD-257/BOD-317) | Hygiene / positioning / launch sequencing | Superseded | SUPERSEDED | NOT_VERIFIED | Superseded by this remediation; file removed from the repo (career-collateral material), private copy retained outside the repo; cited by ADR-030 as historical context only |
| [030](ADR-030-proof-carrying-decision-plane.md) — Proof-carrying decision plane | Context→decision→receipt→proof owned by Verdict | Accepted | PARTIALLY_TRUE | NOT_VERIFIED | — |
| [031](ADR-031-prime-workflow-skills.md) — Prime workflow skills | Resume/hydrate/dispatch/proof/finish leases | Accepted for implementation | PARTIALLY_TRUE | VERIFIED | — |
| [032](ADR-032-core-model-metadata-store.md) — Core owns model metadata | OmniRoute inventory/execute/health only | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [033](ADR-033-shared-memory-provider.md) — Shared memory boundary | Shared recall advisory; local MemoryPlane authoritative | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [034](ADR-034-memory-outbox-mirror-and-fail-open-shared-recall.md) — Outbox mirror | Durable outbox; fail-open shared recall | Accepted | PARTIALLY_TRUE | VERIFIED | — |
| [035](ADR-035-authorized-selected-route-dispatch.md) — Authorized selected-route dispatch | Execution-path strategy authority on the serve path + authorize-only dispatcher binding; demoted selectors | Accepted | PARTIALLY_TRUE | *(new — pending ecosystem re-audit)* | Supersedes ADR-023 |
| [036](ADR-036-goal-to-receipt-orchestration.md) — Goal-to-receipt orchestration | Frontier DAG, capacity-aware eligibility ladder, same-node reassignment, independent OCR review, fail-closed receipt, supervised controller | Accepted | *(new — pending ecosystem re-audit)* | VERIFIED (live runs + fresh-clone certification) | Scope successor to ADR-023 for multi-worker execution |
| [037](ADR-037-supervisor-concurrency-governor.md) — Supervisor concurrency governor | Bounded concurrent stories under one supervisor; `VERDICT_MULTI_STORY` off by default | Accepted | *(new — pending ecosystem re-audit)* | PARTIALLY_TRUE | — |
| [038](ADR-038-improvement-cycle.md) — Measured improvement cycle | `verdict improve`: bounded cycles above a supervised run, measured only from recomputable receipt evidence, deterministic lessons, unchanged-relaunch refusal | Accepted | *(new — pending ecosystem re-audit)* | VERIFIED (offline tests + live cycles 2026-10-12) | Amends ADR-036 |
| [039](ADR-039-passive-inventory-split-authentication.md) — Split passive inventory authentication by API surface | Split passive-identify credentials: inference API key for `/v1/models`, management access token for the five OmniRoute management endpoints | Proposed | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | — |
| 040 — Policy enforcement contract (not on main) | BOD-340 policy enforcement contract — authored on branch `wt/t_b57194bc` (commit `a0aa197e`), not merged to main; number reserved | — | *(reserved; not on main)* | — | — |
| [041](ADR-041-generic-harness-contracts.md) — Generic harness contracts for orchestration | Versioned harness contract layer (`verdict.harness/v1`); adapters translate Verdict decisions, never rank models or broaden admission | Accepted (PR #1025, `e1433271`) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | Extends ADR-036 |
| [042](ADR-042-dynamic-role-model-selection.md) — Dynamic role-based model selection | Pipeline-based selection for roles; shadow evaluation; capacity-aware | Accepted (operator, 2026-11-17) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | Ordering stage amended by ADR-043 |
| [043](ADR-043-role-dependent-selection-order.md) — Role-dependent selection order | Implement/worker nodes: cheapest-sufficient-first (slack before rating); planner and reviewer: strongest-first (rating before slack) | Accepted (operator, 2026-11-20) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | Amends ADR-042 (ordering stage) |
| [044](ADR-044-escalation-after-judged-failures.md) — Escalation after judged failures | After K=2 judged failures the runtime raises the capability floor; tier-0 escalation restricted to SUBSCRIPTION/FREE capacity; no METERED route opened | Accepted (merged in #1182 and #1189) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | — |
| [045](ADR-045-safe-lint-autofix-before-repo-gates.md) — Safe lint autofix before the repo gates | Safe `ruff check --fix` + `ruff format` on owned `.py`/`.pyi` files before verification and gates; records a `lint_autofix` event; reverses the runtime no-autofix policy from PR #811 | Accepted (merged in #1188; operator told 2026-11-20 and did not object) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | — |
| [046](ADR-046-arena-ratings-advisory-source.md) — Arena ratings as advisory reordering source | Exact (org, key) join, VR percentile, role chains, advisory-only integration; never affects admission | CURRENT | *(new)* | — |
| [047](ADR-047-verdict-native-fusion-planning.md) — Verdict-native fusion planning benchmark | `verdict bench fusion-plan` runs a panel of FREE admitted planners from different families, anonymises and synthesises plans via `verdict/fusion_plan_bench.py`; production planning unchanged | Accepted (operator, 2026-11-20) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | Benchmark only; relates to ADR-043 |
| 048 — Unknown is free until proven otherwise (not on main) | Reserved by PR #1242 (story U1, parked); its approach is superseded by ADR-049 | — | *(reserved)* | — | Do not reuse the number |
| [049](ADR-049-cost-policy-and-unknown-handling.md) — Cost policy and unknown-cost handling | A route is PAID only from declared or observed facts, never from its name; per-operator cost order; unknown-cost mode `assume_free` / `probe_only` / `exclude` | Accepted (operator, 2026-11-21) | *(new — pending ecosystem re-audit)* | NOT_VERIFIED | Implemented by CP2 (#1245) and CP1 (#1244) |
| [050](ADR-050-hindsight-memory-of-record.md) — Hindsight is the memory of record | Hindsight replaces ai-memory as the cross-session memory store; ai-memory ref-docs sync chain removed (MEM1); Hermes/harness hooks removed in MEM2 | Accepted (operator, 2026-11-21) | *(new)* | NOT_VERIFIED | Supersedes D1; relates to #1253/#1254 |
| [051](ADR-051-planner-frontier-from-ratings.md) — Planners come from the frontier band of live ratings | Planner pool = top 20% of rated inventory models by fresh `aa_intelligence` (newest fetch, suffix variants once, ties in); one fallback band (top 30%), then fail closed; strongest non-metered first, paid last; doctor warns on empty frontier band | Accepted (operator, 2026-11-23; not yet implemented) | *(new)* | NOT_VERIFIED | Amends ADR-043 (planner order) and ADR-046 (membership, planner only) |

## Duplicate / superseded copies (retain for inbound links)

| File | Lifecycle | Canonical / successor |
|---|---|---|
| `docs/architecture/ADR-EVIDENCE-LEDGER.md` (removed; was a duplicate) | DUPLICATE | [`ADR-001`](ADR-001-evidence-ledger.md) |
| `docs/architecture/ADR-ORCHESTRATOR-ROUTING.md` (removed; was a duplicate) | DUPLICATE | Prefer [`ADR-002`](ADR-002-orchestrator-routing.md); Ruflo framing obsolete |
| [`ADR-ORCHESTRATOR-ROUTING.md`](ADR-ORCHESTRATOR-ROUTING.md) | SUPERSEDED | Ruflo/swarm removal and legacy selector demotion → [`ADR-035`](ADR-035-authorized-selected-route-dispatch.md) |

Byte-identical copies in `verdict-core-memory` are classified DUPLICATE by the cross-repo ADR lifecycle evidence audit;
`verdict-core` `docs/adr/` remains ownership.

## Explicit non-product

| File | Lifecycle | Reason |
|---|---|---|
| This `README.md` | INVALID (as an ADR record) | Index, not a decision — the cross-repo ADR lifecycle evidence audit classification |
| `benchmarks/fixtures/.../ADR-001-spend.md` | INVALID | Benchmark fixture only |

## Missing decisions recorded during reconciliation

Behaviours shipped and referenced by code/docs that still warrant dedicated ADRs
or ecosystem re-proof (not silently inferred as CURRENT):

1. **Session economics STAY/SWITCH** — prompt-cache value / hysteresis after hard eligibility.
2. **Effective capability + context budget** — model + context + tools + decomposition + verification.
3. **Full CURRENT promotion** — requires ecosystem re-audit with executable semantic proof per the cross-repo ADR lifecycle audit evidence bar.

## Ecosystem decision trail

| Decision | Record |
|---|---|
| Provider receipt format | [ADR-021](ADR-021-deterministic-provider-receipts.md) |
| Context provider conformance | [ADR-022](ADR-022-context-provider-conformance.md) |
| SwarmSpec governance (historical) | [ADR-023](ADR-023-governed-swarm-supervision.md) → [ADR-035](ADR-035-authorized-selected-route-dispatch.md) |
| Cross-repo compatibility gate | [ADR-024](ADR-024-cross-repo-compatibility-gate.md) |
| Node envelope enforcement | [ADR-025](ADR-025-node-envelope-enforcement.md) |
| Authoritative V2 lifecycle audit | [verdict-ecosystem ADR_LIFECYCLE](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md) |
