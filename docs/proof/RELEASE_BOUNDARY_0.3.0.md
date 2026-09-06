# Planned v0.3.0 release boundary

**Audit target:** `7dc87a36aaca702a8d7e3eb7bb9de27d9f84be7e`  
**Boundary recorded:** 2026-09-06  
**Current package version:** 0.2.0  
**Status:** Planned boundary; this document is not a release approval or a version bump.

This boundary keeps the next release honest while the portfolio is made application-ready.
It describes what the current `verdict-core` line can demonstrate and what remains future
work in Project #6.

## Included in the v0.3.0 candidate boundary

The candidate may claim the following when the exact release revision passes the required gates:

- Hard eligibility checks run before advisory ranking; an excluded candidate cannot be restored
  by ranking.
- Missing, stale, malformed, and contradictory runtime observations are represented explicitly
  and cannot silently authorize protected work.
- Runtime/capability passport contracts preserve identity, provenance, freshness, authority, and
  limitations for the evidence they receive.
- Policy and legal transition checks are deterministic and versioned.
- Receipts and evidence paths support redaction, integrity checks, scoped persistence, and
  replay/verification according to the existing ADRs.
- Credential-free deterministic fixture/demo paths run without provider credentials or network
  access and explicitly distinguish fixture proof from live execution.
- Local benchmark fixtures and evidence bundles have deterministic, digest-verifiable paths.
- The repository's CI/security workflows are required release checks; a release claim must point
  to the exact successful check record, not only to workflow YAML.
- The public claims ledger and proof matrix identify each claim's status, evidence, limitations,
  observation date, confidence, and public wording.

## Explicitly outside the v0.3.0 boundary

The following are not release claims unless separately implemented, tested, and added to the
ledger with new evidence:

- Current liveness, authorization, quota, or successful execution of every catalog row.
- Production readiness, adoption, model-quality leadership, or performance leadership.
- Sub-5ms lookup, 65% lower memory use, 100,000 messages/second, 82% parser improvement, or
  zero risk-bound breaches without independent reproducible artifacts.
- A mandatory LiteLLM dependency or a shipped LiteLLM custom-router adapter.
- An OmniRoute dependency, internal OmniRoute database integration, or a claim that task-routing
  combos are effective without route diagnostics.
- A public receipt-explorer UI; this is a later Project #6 E4 story.
- Quant-trio consolidation into `verdict-quant`.
- Live-provider quality, latency, availability, or cost claims derived from fixture runs.

## Required release evidence

A v0.3.0 release candidate is not approved until all of the following are recorded against the
exact candidate revision:

1. Full required CI checks pass: tests, lint, type-check, security, install smoke, build,
   contract parity, CodeQL, OSV, SBOM, and dynamic checks where configured.
2. `python scripts/verify_proof_matrix.py` passes.
3. The credential-free demo and documented README command pass from a clean environment.
4. README and proof links resolve; public wording matches the ledger's allowed wording.
5. The release checklist records source revision, command/workflow URL, result, limitation, and
   reviewer/date for every gate.
6. No secrets, private paths, raw prompts, provider credentials, or private exports enter the
   public evidence bundle.

## Publication rule

A claim remains at its current weaker status until its missing evidence is present, reproducible,
dated, and linked to the exact release artifact. A Spec Kit checkbox, issue checkbox, old session
summary, or workflow definition is not release evidence by itself.

Related records: [claims ledger](claims_ledger.v1.json), [proof matrix](proof_matrix.v1.json),
[public evidence index](EVIDENCE_INDEX.md), [ADR-030](../adr/ADR-030-proof-carrying-decision-plane.md),
and [Project #6](https://github.com/users/mrnicholasbcarter-code/projects/6).
