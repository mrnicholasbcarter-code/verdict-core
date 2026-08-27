# ADR-027: Free status is observed, and unretrieved context is disclosed

- **Status:** Accepted — implemented on `feat/verdict-operational-loop`
- **Date:** 2026-08-27
- **Related:** [ADR-019](ADR-019-runtime-negotiated-passports.md), spec 272 FR-032, FR-036

## Context

Verdict's purpose is to extend scarce paid frontier subscription capacity by delegating
legwork to free models. Two defects found by running the shipped code against live gateway
catalogs undermined that purpose.

**Free status was inferred from a missing field.** The kept-set filter excluded a route only
when `pricing` reported a positive price. Against the live catalog at `127.0.0.1:20129`,
**zero of 265 rows carried any `pricing` field at all**, so the paid filter was dead code:
241 of 258 kept identities were treated as free without a single piece of evidence. Absence
of a price is not evidence of free-ness.

**Authoritative context was dropped silently.** `compile_packet_context` defaulted
`governing_doc_paths` to empty, so the ADR slot the delegation thesis depends on was always
empty in production. Worse, `read_selected` swallowed `OSError` and `UnicodeDecodeError`, so a
governing document that could not be read was indistinguishable from one that does not exist.
A weak model cannot compensate for context it was never told was missing.

## Decision

**Free status is a three-valued observation.** `free_status()` returns `free`, `paid`, or
`UNKNOWN`. It is established from an adapter-declared free-tier facet when the gateway exposes
one, or from the identity itself, and never from the absence of a pricing field. `UNKNOWN`
identities remain kept and probe-eligible — a gateway that declares nothing about price must
not empty the catalog — but they never outrank an identity positively known to be free.
This keeps the rule gateway-neutral: no facet is required to exist.

**A source the caller requested and did not receive is recorded, never dropped.** Retrieval
failures become `ContextDecision(action="exclude")` entries carrying a distinguishable reason:
`absent: no such file` versus `unreadable: …`. Governing ADRs are discovered by default from
`docs/adr` using the existing `documentation_preflight._is_adr_path` predicate rather than a
second notion of what counts as authoritative.

Two exclusions are deliberately *not* omissions: a **denied path** is an authority boundary
working as intended, and a **default probe** such as `AGENTS.md` promises nothing in a
repository that has none. Recording either would drown the real signal in noise.

## Consequences

- Live catalog after the change: 265 rows → 258 kept, all 17 positively-free identities ranked
  ahead of 241 `UNKNOWN`, zero opaque aliases. Before: 34 kept, led by the alias `kr/auto`.
- Worker packages now disclose what governing context is missing and why, so a delegated unit's
  failure can be attributed to absent rationale rather than to model weakness.
- `UNKNOWN` free status is honest rather than convenient: preferring positively-free routes is
  a ranking, not an exclusion, so no gateway is locked out for failing to publish pricing.
- Cost claims remain estimates. `tiktoken` is not a dependency; token counts use the existing
  four-characters-per-token approximation and must be described as estimated.
