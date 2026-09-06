# ADR-030: Proof-carrying decision plane for Verdict

- **Status:** Accepted — implementation tracked in GitHub Project #6
- **Date:** 2026-09-06
- **Deciders:** Nick (repo owner)
- **Related:** [ADR-015](ADR-015-evidence-authority-and-portable-receipts.md), [ADR-016](ADR-016-deterministic-policy-and-transition-graphs.md), [ADR-017](ADR-017-durable-privacy-safe-receipt-ledger.md), [ADR-020](ADR-020-gateway-adapter-contracts.md), [ADR-021](ADR-021-deterministic-provider-receipts.md), [ADR-022](ADR-022-context-provider-conformance.md), [ADR-027](ADR-027-observed-free-status-and-context-omissions.md), [ADR-028](ADR-028-launch-gate-tooling.md), [ADR-029](ADR-029-portfolio-repositioning-plan.md); GitHub Project [Verdict Decision Infrastructure](https://github.com/users/mrnicholasbcarter-code/projects/6), issues #450–#468

## Context

Verdict has the important pieces of a governed routing system in separate surfaces: context
packs and memory adapters, eligibility and enforcement gates, provider-neutral gateway
contracts, durable receipts, proof ledgers, and Python/TypeScript contract tests. The wider
portfolio also contains related work in LiteLLM integration, OmniRoute operations, and a
candidate cockpit UI. Without an explicit product boundary, these pieces can drift into
separate projects, duplicate storage, or public claims that are ahead of the code.

The immediate practical objective is twofold:

1. Make the flagship repository credible enough that Nick can apply for AI infrastructure,
   developer-platform, routing, policy, or contract engineering work within five working days.
2. Build a coherent end state for Verdict that can be adopted by existing routing ecosystems
   and can prove what it decided, why it decided it, and what evidence supports that claim.

The governing product question is therefore not only "which model should run this task?" It is:

> What bounded context was available, which candidates were eligible, what decision was made,
> what happened at execution, and can an independent verifier prove the answer without receiving
> the original prompt or credentials?

Existing decisions already establish durable receipt, deterministic-policy, gateway-adapter,
provider-receipt, and context-provider concepts. This ADR composes those authorities into one
vertical product boundary; it does not authorize a parallel memory system or a second routing
kernel.

## Decision

### 1. Verdict owns the proof-carrying decision plane

The canonical flow is:

```text
Task
  -> bounded, provenance-aware context pack
  -> authoritative eligibility set
  -> deterministic fail-closed decision
  -> execution envelope / gateway handoff
  -> privacy-safe receipt
  -> independently verifiable claim and proof
```

Verdict owns the decision, eligibility, receipt, claim, and proof semantics. A gateway may
execute a selected route, but gateway execution does not silently redefine Verdict's policy or
rewrite the decision after the fact.

### 2. Context sources are adapters, not new storage systems

Basic Memory remains the permanent searchable-memory layer for context hydration. Existing
context-mode, codebase-memory, repository evidence, session context, and future sources remain
replaceable adapters. Verdict consumes canonical context items and explicit omission records; it
does not fork those systems or create a new memory database for this product.

A context pack must preserve, where available:

- source identity and provenance reference;
- observed and retrieved timestamps;
- confidence and lifecycle status;
- active, superseded, or disputed state;
- deterministic size/token budget;
- explicit missing, stale, malformed, or unavailable-source reasons; and
- redaction boundaries that exclude credentials and unnecessary personal data.

Active claims may outrank superseded claims only through an explicit deterministic rule.
Disputed claims remain visible and cannot silently satisfy a required fact.

### 3. Eligibility is authoritative and routing fails closed

The selector consumes the authoritative eligible set. It must not re-derive eligibility from a
larger candidate set and thereby restore a candidate excluded by policy. Missing, malformed,
stale, or insufficient context follows a named fail-closed outcome where the policy requires
verification. Every excluded candidate receives a stable reason code suitable for a receipt and
an explanation surface.

Ranking may order eligible candidates. It is not an authority that can admit candidates denied
by policy. Route identity remains gateway-neutral; gateway-specific identifiers and translation
rules belong in adapters.

### 4. Receipts are the portable evidence boundary

Every decision/execution path that claims Verdict authority emits a versioned, privacy-safe
receipt. The receipt records enough information to verify the decision without persisting raw
prompts, API keys, tokens, or unnecessary personal data. At minimum, the model includes the
request/task correlation identifier, policy version, input/context hashes, eligible candidates,
selected route or denial, drop reasons, source references, timestamps, and execution correlation
where a gateway is involved.

Receipt persistence and chain behavior follow ADR-017. Provider evaluation records follow
ADR-021. Receipt verification must fail closed for tampered, malformed, incomplete, skipped, or
unavailable evidence; omission must not become an implicit pass.

### 5. Claims require evidence and preserve history

Public or operational claims use a lifecycle: `active`, `superseded`, `disputed`, or another
explicit status defined by the contract. A claim carries stable identity, text, observation and
verification timestamps, confidence, evidence/receipt references, and supersession relationships.
Superseded evidence is preserved for auditability; it is not silently overwritten or deleted.

A claim is not considered shipped because a Spec Kit checkbox, issue checkbox, old session
summary, or generated report says it is complete. Current source, tests, runtime diagnostics,
version-bound official documentation, GitHub state, and Spec Kit artifacts are reconciled in
that order of authority for task-critical decisions.

### 6. Integrations remain optional boundaries

#### LiteLLM

LiteLLM is the first public distribution adapter. It is optional, must be implemented against a
pinned and documented extension surface, and must not become a mandatory dependency of the
Verdict kernel. A configured LiteLLM request enters the Verdict decision path and correlates its
execution result to the Verdict receipt. When the adapter is absent, normal LiteLLM behavior is
unchanged.

The installed/pinned LiteLLM package documentation and source must be read before implementation;
no custom-router API is inferred from its name or from stale memory.

#### OmniRoute

OmniRoute is an execution and context-handoff adapter, not a replacement for Verdict's decision
kernel. The adapter uses stable API/MCP contracts and correlation identifiers, never private
OmniRoute database tables or local source paths. It distinguishes Verdict's decision from
OmniRoute's execution result and reports timeout, unavailable-gateway, malformed-response, and
stale-catalog outcomes explicitly.

Operational routing policy may prefer healthy free models for ordinary coding, background, and
summarization work, while reserving premium routes for design, orchestration, and review. That
policy is an observable configuration/diagnostic concern, not an undocumented core invariant.

### 7. The UI follows the receipt contract

The default public cockpit direction is a Verdict receipt explorer. It consumes the canonical
receipt/proof schema, supports fixture mode without credentials or live services, and shows
accepted routes, denied routes, reasons, freshness, provenance, and verification status. An
OmniRoute configuration dashboard remains a possible later internal/operator surface, but it is
not the first public product slice and does not block the core vertical slice.

### 8. Delivery is staged around an application checkpoint

Implementation is tracked in private GitHub Project #6 as four epics:

- **E1 — Apply-ready flagship:** claim inventory, recruiter README, no-key proof demo, proof
  matrix, and case study.
- **E2 — Proof-carrying decision plane:** context pack, provider conformance, eligibility,
  fail-closed routing, receipts, and claims.
- **E3 — Integration distribution:** LiteLLM, OmniRoute, operational policy, and contract parity.
- **E4 — Demonstration and launch:** reproducible benchmark, receipt explorer, and launch package.

Nick begins applying after the E1 checkpoint. Stars, the quant-trio merge, cockpit rebuild,
complete memory platform, LiteLLM adapter, and launch day do not block applications.

## Alternatives considered

### Create a new `verdict-decision-plane` repository

**Rejected:** A new repository would split the canonical contract from the existing routing,
receipt, proof, and test surfaces and would create another synchronization boundary. Keep
`verdict-core` authoritative until a future extraction has a demonstrated adoption/ownership
reason.

### Make Verdict depend on a new unified memory database

**Rejected:** The workspace already has searchable memory, code graph, session context, and
repository evidence systems. A new database would duplicate retrieval and increase operational
surface. Use conformance adapters and keep provider-specific storage outside the decision kernel.

### Let LiteLLM or OmniRoute own the decision

**Rejected:** Existing gateways are valuable distribution/execution surfaces, but making either
one authoritative would make Verdict's fail-closed, reason-coded, proof-carrying behavior
provider-specific. Verdict remains the policy/decision authority; adapters translate at the
boundary.

### Build the cockpit before the core proof slice

**Rejected:** A UI without canonical receipts would invent a second event model and risk
presenting unsupported claims. Build the offline decision-to-proof slice first, then make the
cockpit a consumer of real receipts.

### Optimize for 40k stars before applying

**Rejected:** The portfolio review found no realistic solo-dev path to 40k stars in twelve
months without a major coattail event. A recruiter-readable flagship, verifiable evidence, and
a clear technical case study produce near-term job value; distribution and stars are later
outcomes.

## Consequences

### Positive

- One coherent story connects context hydration, routing, receipts, proof, and integrations.
- The first vertical slice can run offline without provider credentials.
- Claims become auditable instead of relying on README trust.
- LiteLLM and OmniRoute provide distribution without owning the core contract.
- A receipt explorer becomes a real proof surface rather than a speculative dashboard.
- The five-day application objective is protected from longer product work.

### Costs and risks

- Contract changes may require synchronized Python JSON schemas and TypeScript zod changes;
  parity tests are mandatory.
- Context freshness, disagreement, and redaction add implementation and test work.
- Independent verification limits what can be stored in a receipt and may require hashes or
  evidence references instead of raw payloads.
- LiteLLM and OmniRoute APIs can change; each adapter must pin/document the inspected surface.
- The plan deliberately postpones broad experimental-module cleanup and quant consolidation
  from the application-critical path.

## Implementation governance

Each Project #6 story must contain acceptance criteria, focused validation, dependencies, likely
files, evidence required to close, and an explicit not-in-scope boundary. Before implementation:

1. Read the nearest applicable constitution and the active Spec Kit artifacts.
2. Reconcile current source, tests, runtime diagnostics, GitHub state, and official installed
   library documentation.
3. Create or update the feature's Spec Kit artifacts in the repository's canonical location.
4. Run the Spec Kit sequence: constitution -> specify -> clarify -> analyze -> plan -> tasks ->
   implement -> checklist.
5. Record changed decisions in a new ADR or issue comment; do not silently rewrite history.

The Project #6 charter and issues #450–#468 are the operational task index. This ADR is the
architecture boundary; repository-local Spec Kit artifacts remain the implementation record.
