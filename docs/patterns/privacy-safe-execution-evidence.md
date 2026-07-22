# Pattern: Privacy-Safe Execution Evidence

**Status:** adopted for issue #53
**Related ADR:** [`ADR-001`](../architecture/ADR-EVIDENCE-LEDGER.md)
**Source patterns:** Ruflo ADR-103, ADR-131, ADR-144, ADR-171, and ADR-176

## Intent

Make a routing decision and the facts observed while executing it replayable
without persisting user prompts, completions, credentials, or unverifiable
claims.

## Shape

```text
decision snapshot
  -> execution_started
  -> append-only transport lifecycle events
  -> exactly one terminal event
  -> explain envelope selected by opaque server ID
```

The decision snapshot is immutable. Lifecycle events are append-only. A
terminal event is accepted once; late cancellation/close signals are recorded
as duplicate-finalization metrics rather than replacing the terminal result.

## Applicability

Use this at gateways, worker dispatch boundaries, and workflow controllers
where operators need to explain selection and transport behavior but must not
retain raw task content.

Do not use it as a substitute for task correctness verification, durable audit
storage, tenant identity, or a quality judgment. Those require stronger
receipts and an explicit backend/policy.

## Invariants

- server-owned opaque IDs are storage keys;
- caller IDs are bounded correlation metadata only;
- redaction precedes persistence;
- decision-time candidates are copied once, never recomputed on lookup;
- every stream termination path closes upstream resources;
- terminalization is idempotent;
- unverifiable fields remain explicitly unobserved;
- lookup is authorization-scoped and scope mismatches are indistinguishable
  from missing records.

## Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| duplicate request IDs cross-link records | opaque server IDs and per-record finalization keys |
| prompt or secret leakage | protocol-only feature extraction and pre-storage redaction |
| stale eligibility changes the explanation | immutable decision snapshot |
| disconnect leaks an upstream stream | `finally` cleanup plus explicit close tests |
| HTTP success is mistaken for task success | transport outcome is separate from verification/quality |
| process restart loses evidence | advertise process-local limitations; follow with durable adapter |

## Verification evidence

Adopters should retain fixture-driven schema checks, lifecycle property tests,
resource-close assertions, authorization-scope tests, and a replay receipt
containing the code SHA and contract version. Positive learning labels must be
admitted only from independently verified outcomes, following the provenance
rule in Ruflo ADR-171.
