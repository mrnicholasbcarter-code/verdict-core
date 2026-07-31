# Public evidence index

This index is the one-page entry point for the #134 proof matrix. It describes
what the current `verdict-core` checkout proves, what it only observes, and
what it deliberately refuses to claim.

## Snapshot

| Field | Value |
| --- | --- |
| Repository | `mrnicholasbcarter-code/verdict-core` |
| Audited source commit | `36e2546079a580e1e7be9e3ec82a8354b81c2dcd` |
| Freeze date | 2026-07-31 |
| Matrix | [`proof_matrix.v1.json`](proof_matrix.v1.json) |
| Claims ledger | [`claims_ledger.v1.json`](claims_ledger.v1.json) |
| Redaction policy | [`REDACTION_POLICY.md`](REDACTION_POLICY.md) |
| Adversarial checklist | [`ADVERSARIAL_REVIEW_CHECKLIST.md`](ADVERSARIAL_REVIEW_CHECKLIST.md) |
| Validator | `python scripts/verify_proof_matrix.py` |

## Verified local contracts

- Hard eligibility is applied before advisory ranking; excluded candidates
  cannot be reintroduced.
- Stale, missing, malformed, and contradictory runtime evidence fails closed.
- Capability and runtime passports preserve exact identity, authority,
  freshness, and limitations.
- Runtime compatibility reports are deterministic, fail-closed, and
  secret-safe when built from existing passport evidence.
- Policy transitions, durable receipts, evaluation promotion, and the
  credential-free demo have focused tests and versioned contracts.
- Reproducible benchmark fixtures and content-addressed evidence bundles have
  local verification paths.

## Observed or partial evidence

- The 2026-07-28 and 2026-07-29 OmniRoute catalog records are bounded historical
  observations. Their own limitations say catalog membership is not liveness,
  authorization, quota, or eligibility.
- CI workflow definitions cover test, lint, type, security, install, build, and
  CodeQL paths. A workflow definition is not a successful run; exact PR checks
  must be attached to a release change.
- Release gates are defined, but the current matrix does not mark the complete
  tagged-release gate set as passed.

## Explicitly not approved

The ledger does not approve unsupported quantitative portfolio claims such as
sub-millisecond or sub-five-millisecond performance, percentage improvements,
100,000-message throughput, zero risk-bound breaches, adoption counts, or
production readiness without a reproducible artifact that defines the metric,
baseline, environment, date, and raw result.

Private database exports, credentials, account identifiers, raw prompts, raw
tool/resource content, and private endpoint details are outside the public
bundle. Their absence is a security boundary, not missing proof to be silently
filled with assumptions.
