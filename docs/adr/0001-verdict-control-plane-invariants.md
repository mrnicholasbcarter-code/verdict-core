# ADR-0001: Verdict control-plane architectural invariants

Status: Accepted

## Decision

- Verdict optimizes complete execution paths, not merely model selection.
- Context Intelligence is Core-owned; Serena, Codebase Memory, LSPs, Context7, and future MCPs are replaceable semantic providers.
- A useful native intelligence baseline must work when external providers are unavailable.
- Hard eligibility precedes ranking/optimization; unknown does not mean healthy, capable, or free.
- Gateways provide inventory, transport, health, quota, and evidence; they do not own routing strategy.
- Effective capability is model + context + tools + decomposition + verification/recovery.
- Durable execution state lives in Git + worktree + Linear + proof state + .verdict/handoff.md, not proprietary agent chat history.
- One active implementation story owns one worktree/branch; Cursor, Claude Code, Codex, and Prime are replaceable workers.
- Learning/bandit/SONA policy may rank already-qualified options but cannot override hard gates.

## Consequences

- Cheap models can be made more capable through precise context and decomposition without weakening correctness gates.
- Semantic providers can be upgraded or replaced without changing Core planning contracts.
- Execution decisions must remain provenance-rich, replayable, and independently verifiable.
