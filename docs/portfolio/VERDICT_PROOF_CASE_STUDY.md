# Verdict: a proof-carrying decision plane

## One-minute summary

Verdict is a Python decision layer for AI execution. It does not claim that a
catalog row is healthy, that a model is qualified because its name looks right,
or that a recommendation is proof of what happened. It separates:

1. bounded context and runtime evidence;
2. hard eligibility gates;
3. advisory ranking among candidates that survived those gates;
4. privacy-safe receipts; and
5. claims that point back to independently inspectable evidence.

The repository's public proof path runs without provider credentials or network
access. It is a deterministic fixture, not a production deployment or a live
provider benchmark.

## The engineering problem

A conventional model router can rank a stale, unknown, or policy-excluded
candidate if ranking is allowed to outrank policy. A post-hoc cost dashboard
can report spend but cannot explain why a route was chosen. A catalog can list a
model without proving current liveness, authorization, quota, or capability.

Verdict addresses those failure modes by making the eligible set authoritative:
excluded candidates cannot be reintroduced by later ranking, unknown runtime
truth is explicit, and each exclusion has a stable reason. The route identity
and receipt remain inspectable after the decision.

## What is implemented

### Context and runtime evidence

Capability and runtime passports preserve exact identity, provenance, freshness,
authority, and limitations. Runtime compatibility reports are deterministic and
secret-safe when rendered from existing passport evidence.

- Implementation: `verdict/runtime_passports.py`,
  `verdict/runtime_compatibility.py`
- Tests: `tests/test_runtime_passports.py`,
  `tests/test_runtime_compatibility.py`
- Public boundary: these contracts do not execute a protocol or certify an
  external service by themselves.

### Eligibility before ranking

Hard policy, freshness, capability, security, privacy, quota, and cost gates run
before advisory ranking. A candidate removed by the gate cannot return through a
score or recommendation.

- Implementation: `verdict/eligibility.py`
- Tests: `tests/test_eligibility_gate.py`, `tests/test_adaptive_ranker.py`
- Verification:
  `pytest -q tests/test_eligibility_gate.py tests/test_adaptive_ranker.py`

### Receipts and privacy

Receipt and evidence contracts record decision facts, selected routes, drop
reasons, timestamps, and integrity information without storing the full prompt
or credentials. Retention and access-control decisions remain deployment
responsibilities rather than implied product guarantees.

- Implementation: `verdict/receipt_store.py`, `verdict/evidence_receipts.py`
- Tests: `tests/test_durable_receipts.py`, `tests/test_security.py`,
  `tests/test_evidence.py`
- Threat model: `docs/THREAT_MODEL_RECEIPTS.md`

### Credential-free proof

The flagship quickstart uses checked-in fixture data. It names the selected
route, receipt, and exclusions and does not call a provider.

```bash
pip install verdict-core
verdict quickstart --non-interactive --dry-run
```

The current README and regression tests keep the documented output aligned with
the fixture implementation. This demonstrates a local contract; it does not
prove live-provider availability, adoption, production readiness, or model
quality.

## Verification packet

The public proof matrix and claims ledger are the source of truth for claim
status and limitations:

- [Portfolio proof matrix](PORTFOLIO_PROOF_MATRIX.md)
- [Claims ledger](../proof/claims_ledger.v1.json)
- [Proof matrix](../proof/proof_matrix.v1.json)
- [Evidence index](../proof/EVIDENCE_INDEX.md)
- [Redaction policy](../proof/REDACTION_POLICY.md)
- [Release boundary](../proof/RELEASE_BOUNDARY_0.3.0.md)

Validate the checked-in evidence from a clean checkout:

```bash
python scripts/verify_proof_matrix.py
pytest -q tests/test_proof_matrix.py
```

The validator checks that evidence paths exist and that public wording does not
contain secret-bearing material. It does not turn an observed or partial row
into a verified claim.

## What remains deliberately unclaimed

This case study does not claim:

- that every listed gateway route is runnable;
- production scale, adoption, or readiness;
- a measured cost reduction from live provider invoices;
- model-quality improvement;
- quantitative portfolio performance without a reproducible artifact; or
- that local fixture results substitute for live execution evidence.

Those boundaries are part of the engineering result. A reviewer can inspect the
source, focused tests, evidence rows, and limitations without trusting a
marketing statement alone.

## Job-search case-study framing

**Problem:** model routing needs policy, freshness, and explanation rather than
an opaque winner.

**Decision:** keep Verdict as the authority for eligibility and receipts; keep
provider gateways and memory/context systems as replaceable boundaries.

**Implementation:** use versioned contracts, deterministic gates, named drop
reasons, redaction rules, and independently inspectable proof artifacts.

**Verification:** run the credential-free quickstart, focused gate/receipt tests,
and the proof validator. For live-provider claims, require fresh direct evidence
and label unavailable evidence as blocked rather than inferring success.

**Known limitation:** the public repository proves local behavior and records
bounded observations. It does not pretend that fixture evidence is a live
service or a production measurement.
