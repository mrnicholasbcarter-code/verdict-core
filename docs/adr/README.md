# Architecture Decision Records

Every significant architectural decision in `verdict-core` is recorded here. Each record
states the context that forced a choice, the decision itself, and the consequences the
project accepted along with it. ADRs are append-only: a decision that no longer holds is
superseded by a later record rather than edited away.

## How to read this index

- **Status** is copied from each record's own `Status` field. `Accepted` means the decision
  stands; `Proposed` means it is agreed in principle but not yet implemented; `Partially
  implemented` means some surfaces conform and others do not, and the record says which.
- A partially-implemented ADR is a commitment, not a description of today's code. Check the
  record's own status line before assuming a behaviour is live.

## Adding a new ADR

1. Take the next free number — check the highest existing file, not the last one you
   remember. Two records were numbered ADR-020 at one point precisely because that check
   was skipped.
2. Name the file `ADR-0NN-short-kebab-title.md` and open it with an `# ADR-0NN: Title` H1.
3. Include `Status`, `Date`, and `Deciders` fields, then `Context`, `Decision`, and
   `Consequences` sections.
4. Cross-reference related records with `Supersedes`, `Amends`, or `Related` fields, and add
   a row to the table below.

## The records

| ADR | Decision | Status |
|---|---|---|
| [001](ADR-001-evidence-ledger.md) — Versioned, privacy-safe execution evidence | Execution evidence is a versioned, tagged envelope; payloads never enter the ledger. | Accepted |
| [002](ADR-002-orchestrator-routing.md) — Orchestrator-driven routing | The orchestrator selects for non-protected work; Verdict stays a thin deterministic gate. | Accepted |
| [003](ADR-003-platform-neutral-guidance-boundary.md) — Platform-neutral guidance boundary | Guidance is an optional in-process feature behind an explicit boundary, never in the enforcement path. | Proposed |
| [004](ADR-004-local-first-memory-plane.md) — Local-first memory plane | A versioned `MemoryPlane` contract with SQLite as the durable source of truth. | Accepted |
| [005](ADR-005-code-intelligence-graph-memory-bridge.md) — Code intelligence graph and memory bridge | Symbol and graph summaries are ingested lightweightly rather than mirroring whole codebases. | Approved |
| [006](ADR-006-authoritative-documentation-preflight.md) — Authoritative documentation preflight | A deterministic documentation preflight runs at the MemoryPlane boundary before implementation. | Accepted |
| [007](ADR-007-omniroute-catalog-qualification.md) — OmniRoute catalog qualification | Catalog snapshots qualify as sanitized deterministic summaries, separately from liveness. | Accepted |
| [008](ADR-008-global-runtime-ownership.md) — Global runtime ownership | One versioned contract owns global runtime state explicitly rather than by convention. | Proposed |
| [009](ADR-009-durable-memory-write-gate.md) — Durable memory write gate | All lifecycle and session memory writes pass through `verdict.memory_gate.MemoryGate`. | Accepted |
| [010](ADR-010-fail-closed-capability-passports.md) — Fail-closed capability passports | Qualification is a versioned passport for one exact executable route; absence denies. | Accepted |
| [011](ADR-011-omniroute-catalog-qualification-baseline.md) — Catalog qualification baseline | The catalog baseline (identity and claimed metadata) stays separate from route qualification. | Accepted |
| [012](ADR-012-consented-budgeted-probes.md) — Consented, budgeted probes | Live qualification probes require explicit operator consent and a spend budget. | Accepted |
| [013](ADR-013-independent-protocol-surface-qualification.md) — Independent protocol surface qualification | Chat Completions and Responses are qualified independently with hermetic probe cases. | Proposed |
| [014](ADR-014-tool-and-structured-output-qualification.md) — Tool and structured-output qualification | Strict structured output and tool lifecycles are qualified as separate capabilities. | Accepted |
| [015](ADR-015-evidence-authority-and-portable-receipts.md) — Evidence authority and portable receipts | Evidence for an exact executable route is split into two related, portable records. | Accepted |
| [016](ADR-016-deterministic-policy-and-transition-graphs.md) — Deterministic policy and transition graphs | A versioned hard-policy document compiles before any ranking or execution occurs. | Accepted |
| [017](ADR-017-durable-privacy-safe-receipt-ledger.md) — Durable privacy-safe receipt ledger | A local SQLite ledger in WAL mode is the canonical receipt persistence boundary. | Accepted |
| [018](ADR-018-shadow-and-counterfactual-evaluation.md) — Shadow and counterfactual evaluation | Evaluation artifacts are versioned and payload-free; every observation binds to evidence. | Accepted |
| [019](ADR-019-runtime-negotiated-passports.md) — Runtime-negotiated passports | A versioned `RuntimeCapabilityPassport` records what a tool or protocol peer negotiated at runtime. | Accepted |
| [020](ADR-020-gateway-adapter-contracts.md) — Gateway adapter contracts | A versioned, provider-neutral adapter contract keeps gateway specifics out of core. | Accepted |
| [021](ADR-021-deterministic-provider-receipts.md) — Deterministic provider receipts | All domain providers emit a standardized portable `ProviderReceipt` payload. | Accepted |
| [022](ADR-022-context-provider-conformance.md) — Context provider conformance suite | A shared conformance suite pins context-provider behaviour across repos. | Accepted (partial) |
| [023](ADR-023-governed-swarm-supervision.md) — Governed swarm supervision | Swarm supervision is pinned by conformance tests so agents cannot bypass governance. | Accepted (partial) |
| [024](ADR-024-cross-repo-compatibility-gate.md) — Cross-repo compatibility gate | A compatibility manifest plus a fail-closed gate CLI guards cross-repo contract drift. | Partially implemented |
| [025](ADR-025-node-envelope-enforcement.md) — Node envelope enforcement | `verdict-node` enforces the same `ExecutionEnvelope` invariants as core. | Proposed |
| [026](ADR-026-responses-compatibility-boundary.md) — Responses compatibility boundary | A versioned compatibility rule applies immediately before the HTTP Responses transport. | Accepted |
| [027](ADR-027-observed-free-status-and-context-omissions.md) — Observed free status and context omissions | Free status is observed (`free`/`paid`/`UNKNOWN`), never inferred from a missing price; a requested context source that could not be read is disclosed with a reason. | Accepted |

## Ecosystem decision trail

The five ecosystem integration decisions are recorded across the numbered trail above rather
than in a separate series:

| Decision | Record |
|---|---|
| Provider receipt format | [ADR-021](ADR-021-deterministic-provider-receipts.md) |
| Context provider interface standardization | [ADR-022](ADR-022-context-provider-conformance.md) |
| SwarmSpec governance model | [ADR-023](ADR-023-governed-swarm-supervision.md) |
| Verdict-ecosystem as extension, not fork | [ADR-024](ADR-024-cross-repo-compatibility-gate.md) |
| Node envelope enforcement | [ADR-025](ADR-025-node-envelope-enforcement.md) |

## Un-numbered records

Two records predate the current numbering scheme and are still referenced by the specs:

- `ADR-ORCHESTRATOR-ROUTING.md` — the fuller orchestrator/gate boundary record that
  `docs/specs/ROUTING_POLICY.md` and `docs/specs/ENFORCEMENT_AND_LEARNING.md` point at.
  Overlaps [ADR-002](ADR-002-orchestrator-routing.md), which it postdates.
- `docs/architecture/ADR-EVIDENCE-LEDGER.md` — sits outside `docs/adr/` and amends the
  record above.

Both need renumbering into the main sequence with their inbound references updated. That
touches the specs, so it is tracked separately rather than folded into this index.
