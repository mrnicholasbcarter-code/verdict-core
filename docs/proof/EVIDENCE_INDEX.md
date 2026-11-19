# Public evidence index

This index is the entry point for the #134 proof matrix. This audit revises
claim wording against source commit `8b1f9d8e82fd7853cdffe19aa1b49baac32229ed`
on 2026-10-08, not against every later checkout. Historical run artifacts retain
their own execution revisions; they are not re-executions at the audited SHA.
A structural validator pass checks JSON shape and referenced paths, not the
semantic truth, source symbols, run provenance, or expired entry reviews.

## Snapshot

| Field | Value |
| --- | --- |
| Repository | `mrnicholasbcarter-code/verdict-core` |
| Source revision for this wording audit | `8b1f9d8e82fd7853cdffe19aa1b49baac32229ed` |
| Wording audit date | 2026-10-08 |
| Historical claim freeze date | 2026-09-06 |
| Per-entry review deadlines | 10 of 10 expired 2026-10-06; no dates renewed without exact-source re-review |
| Matrix | [`proof_matrix.v1.json`](proof_matrix.v1.json) |
| Claims ledger | [`claims_ledger.v1.json`](claims_ledger.v1.json) |
| Redaction policy | [`REDACTION_POLICY.md`](REDACTION_POLICY.md) |
| Adversarial checklist | [`ADVERSARIAL_REVIEW_CHECKLIST.md`](ADVERSARIAL_REVIEW_CHECKLIST.md) |
| Validator | `python scripts/verify_proof_matrix.py` |

## Conversion assets

- [2026-09-06 claims audit](CLAIMS_AUDIT_2026-09-06.md) records **historical**
  claim statuses and the v0.3.0 candidate release boundary at its own SHA.
  It is not a current approval or replacement for expired per-entry reviews.
- [v0.3.0 release boundary](RELEASE_BOUNDARY_0.3.0.md) defines what may be
  claimed and what remains explicitly outside the candidate release.

The portfolio/career-collateral case studies formerly linked here
(`docs/portfolio/`) were removed from the repository (BOD-257/BOD-317); a
private copy is retained outside the repo for historical reference, not for
publication.

## Verified local contracts

- Hard eligibility is applied before advisory ranking; excluded candidates
  cannot be reintroduced.
- Malformed and contradictory runtime observations have explicit local handling.
  The cache can serve a stale report inside its TTL plus 30-second grace window,
  including after refresh failure. The eligibility gate does not recheck age
  before admitting a cached eligible/READY report. Do not claim a universal
  stale-protected-work fail-closed guarantee.
- Capability and runtime passports preserve exact identity, authority,
  freshness, and limitations.
- Runtime compatibility reports are deterministic, fail-closed, and
  secret-safe when built from existing passport evidence.
- Policy transitions, configured durable receipts, standalone evaluation
  decisions, and the credential-free demo have focused local contracts.
  The adaptive ranker canary is **not** wired to evaluation approval.
- Benchmark fixture structure and digests can be checked locally; measured
  timings and threshold verdicts vary. Evidence bundle bytes are stable only
  when collected artifact bytes are identical.

## Observed or partial evidence

- The 2026-07-28 and 2026-07-29 OmniRoute catalog records are bounded historical
  observations. Their own limitations say catalog membership is not liveness,
  authorization, quota, or eligibility.
- CI workflow definitions cover test, lint, type, security, install, build, and
  CodeQL paths. PR/main security jobs are blocking, but tag release does not
  require all exact successful SAST/dependency/CodeQL jobs. A workflow
  definition is not a successful exact-SHA run.
- The matrix has 20 rows (15 verified, 4 partial, 1 observed) after removing
  the portfolio-claims row for the career-collateral case study deleted from
  the repository (BOD-257/BOD-317). The ledger has 10 entries (5 verified,
  2 observed, 2 unsupported, 1 aspiration) after removing the matching
  self-reported entry for the same reason. **Every remaining entry review
  deadline expired on 2026-10-06**; status labels are historical until
  exact-source re-review.
- The supervisor detects **controller-progress** stalls; a two-node injected
  stall proof and a separate three-node resume run are retained. Context
  overflow is request-scoped, not provider cooldown; live multi-provider
  failover exists for other recorded faults, not a live context-overflow chain.
- Multi-story governor behavior applies only when `VERDICT_MULTI_STORY=on`.
  Node outcome sidecar JSONL is not digest-bound by the run receipt.
- [Live orchestrate run 20261119T000727Z](live-run-20261119T000727Z/README.md)
  (LIVE, 2026-11-19, source `d5e88ee8`): one free worker attempt, open-code-review
  PASS on a different family, receipt integrity OK. Evidence record only: the
  digest-bound event log and receipt hold host paths and are retained privately,
  with their digests recorded. It used existing route-health state, the
  reviewer's two non-blocking comments were dropped (#1137), and it is one run
  with no speed or savings claim. Demo replay:
  [`live-run.svg`](../assets/live-run.svg).
- [Live orchestrate run 20261119T130739Z](live-cockpit-20261119T130739Z/README.md)
  (LIVE, 2026-11-19, source `35c110c1`, free capacity): four nodes; a real
  HTTP 429 and a failed test on `cerebras/gpt-oss-120b`, both nodes reassigned
  to `nvidia/google/gemma-4-31b-it`; open-code-review PASS on a different
  family; receipt integrity OK. Evidence record only (the digest-bound event log
  holds host paths and is retained privately, digests recorded). One run, no
  speed or savings claim. Cockpit replay (README hero):
  [`live-cockpit.svg`](../assets/live-cockpit.svg).

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
