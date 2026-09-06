# Public claims audit — 2026-09-06

**Audited revision:** `7dc87a36aaca702a8d7e3eb7bb9de27d9f84be7e`  
**Repository:** `mrnicholasbcarter-code/verdict-core`  
**Auditor:** Verdict maintainer  
**Purpose:** E1-S1 / Project #6 — establish the public claim inventory and the v0.3.0 release boundary.

## Result

The repository already has a functioning claims ledger and proof matrix. This audit refreshed
their source revision and review date, verified every referenced evidence path exists at the
audited revision, and classified the public surfaces below. No routing behavior was changed.

| Artifact | Result |
|---|---|
| Claims ledger | 11 entries; 6 verified, 1 observed, 1 self-reported, 2 unsupported, 1 aspiration |
| Proof matrix | 15 rows; 11 verified, 1 observed, 2 partial, 1 blocked |
| Evidence paths | 0 missing paths across both artifacts |
| README relative links | 0 missing targets |
| Proof validator | Passes after metadata refresh |
| Public boundary | Recorded in `RELEASE_BOUNDARY_0.3.0.md` |

## Authority order

Task-critical claim status was evaluated using this order:

1. Current source and tests at the audited revision.
2. Current CI/runtime evidence where available.
3. Version-bound documentation and schemas.
4. GitHub state and release metadata.
5. Older evidence, session notes, and task checkboxes only as pointers.

A checked task or issue is not treated as implementation proof. Historical claims remain in the
ledger when useful, but their allowed public wording is bounded by their status and missing
evidence.

## Current claim inventory

| IDs | Status | Public treatment |
|---|---|---|
| CL-001–CL-006 | `verified` | May use the ledger's allowed wording, limited to the cited local contracts, fixtures, and tests. |
| CL-007–CL-008 | `unsupported` | Do not publish the stronger catalog-runnability or quantified-memory wording; use each downgrade wording. |
| CL-009 | `self_reported` | May describe the portfolio case study as self-reported; do not present its metrics as independently verified. |
| CL-010 | `aspiration` | Do not claim production readiness or that every release gate has passed. |
| CL-011 | `observed` | May describe the security/privacy gate as wired and tested, while disclosing that a complete tagged-release/DAST proof is still missing. |

## Public surfaces reviewed

- `README.md`: installation, offline fixture, live-probe boundary, routing invariants, cost demo,
  context-lift receipt, test/gate summary, architecture links, and project status.
- `pyproject.toml`: package identity, version `0.2.0`, description, supported Python versions,
  optional dependencies, and entry point.
- `docs/proof/claims_ledger.v1.json`: claim text, status, allowed wording, evidence, dates,
  confidence, objections, falsification tests, and missing evidence.
- `docs/proof/proof_matrix.v1.json`: requirement status, evidence locators, verification command,
  public wording, and gap.
- `docs/proof/EVIDENCE_INDEX.md`: recruiter/reviewer entry point and explicit non-approved claims.
- `docs/portfolio/PORTFOLIO_PROOF_MATRIX.md`: audience-specific proof assets and boundaries.
- `docs/benchmarks/routing-demo.md`: mock/live/recorded modes and the no-provider-spend boundary.
- `docs/benchmarks/context-lift.md` and its receipt: paired live proof, blocked behavior, omissions,
  and secret-stripping limits.
- `RELEASE_CHECKLIST.md`, `ACCEPTANCE_GATES.md`, and `VERSIONING.md`: release obligations and
  the distinction between defined gates and passed evidence.

## Findings and action

1. **No evidence path is missing.** Every ledger and matrix path resolves to a tracked file at
   the audited revision.
2. **The existing downgrade language is retained.** Unsupported catalog-runnability, quantified
   memory performance, self-reported trading results, and production-readiness claims remain
   weaker than their historical claim text.
3. **Fixture/live boundaries are explicit.** The README and benchmark docs distinguish fixture
   runs from live-provider execution; blocked live paths do not produce a pass.
4. **The source revision was stale.** Both proof artifacts previously pointed at
   `36e2546079a580e1e7be9e3ec82a8354b81c2dcd` and a 2026-07-31 freeze. They now point at the
   audited 2026-09-06 revision and have a 2026-10-06 review date.
5. **The release boundary is not a release approval.** v0.3.0 remains a candidate boundary until
   the exact candidate revision passes the required CI/security/install/documentation checks.

## Validation commands

```bash
python scripts/verify_proof_matrix.py
pytest -q tests/test_proof_matrix.py
```

The full repository CI matrix remains the release authority. This audit does not claim live
provider quality, production readiness, adoption, performance leadership, or successful execution
of every catalog entry.

## Related records

- [Claims ledger](claims_ledger.v1.json)
- [Proof matrix](proof_matrix.v1.json)
- [Public evidence index](EVIDENCE_INDEX.md)
- [v0.3.0 release boundary](RELEASE_BOUNDARY_0.3.0.md)
- [Adversarial review checklist](ADVERSARIAL_REVIEW_CHECKLIST.md)
- [ADR-030: Proof-carrying decision plane](../adr/ADR-030-proof-carrying-decision-plane.md)
- [Project #6](https://github.com/users/mrnicholasbcarter-code/projects/6)
