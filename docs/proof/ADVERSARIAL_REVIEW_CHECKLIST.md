# Adversarial proof review checklist

Use this checklist before changing a claim from `unsupported`, `self_reported`,
`aspiration`, `partial`, or `blocked` to stronger wording. The reviewer should
try to falsify the claim, not merely confirm that a plausible file exists.

## Traceability

- [ ] Is the claim uniquely identified in `claims_ledger.v1.json`?
- [ ] Does every evidence path exist at the frozen commit?
- [ ] Does the locator identify a test, schema, implementation, or artifact that
  actually bears on the claim?
- [ ] Is the evidence authored, observed, self-reported, inferred, or external?
- [ ] Is the claim date-bounded and is `review_after` still valid?

## Reproduction

- [ ] Is there a bounded command or procedure a skeptical reviewer can run?
- [ ] Does it use checked-in fixtures or clearly declare external dependencies?
- [ ] Are environment, dataset, baseline, warmup, repetitions, and metric
  definitions recorded for every quantitative result?
- [ ] Does repeating the procedure produce the same decision, digest, or result?
- [ ] Does the test prove the required invariant rather than only exercise a
  happy path?

## Failure and adversarial cases

- [ ] Try stale, missing, malformed, contradictory, expired, and unauthorized
  evidence. Does the result remain unknown or blocked?
- [ ] Try reordered inputs, duplicate identities, aliases, and mismatched
  routes. Does identity remain exact and deterministic?
- [ ] Try an injected instruction in a retrieved document or tool result. Can
  it change policy, eligibility, or evidence authority?
- [ ] Try a post-byte transport failure. Is cross-route fallback refused when
  it would make the result unauditable?
- [ ] Try a secret-bearing field, private path, raw prompt, endpoint, token, or
  account identifier. Is it rejected or redacted without changing status?
- [ ] Try to make an advisory ranker, memory result, catalog row, or narrative
  override a hard gate. Does the gate remain authoritative?

## Publication decision

- [ ] The allowed wording is no stronger than the evidence.
- [ ] Limitations and negative results are visible next to the claim.
- [ ] The claim does not imply production readiness, adoption, quality,
  performance leadership, or live availability without the required artifact.
- [ ] Authorship and upstream/generated material are separated.
- [ ] The public substitute contains no secrets, raw account exports, or private
  paths.
- [ ] A second reviewer can reproduce the status from the ledger and matrix.

Any unchecked item keeps the claim at its current weaker status.
