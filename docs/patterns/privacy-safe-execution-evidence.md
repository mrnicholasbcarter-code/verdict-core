# Pattern: Privacy-Safe Execution Evidence

**Status:** adopted for issue #53
**Related ADR:** [`ADR-001`](../architecture/ADR-EVIDENCE-LEDGER.md)

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
terminal event is accepted once; late cancellation or close signals are
recorded as duplicate-finalization metrics rather than replacing the terminal
result.

## Applicability

Use this at gateways, worker execution boundaries, and workflow controllers
where operators need to explain selection and transport behavior but must not
retain raw task content.

Do not use it as a substitute for task-correctness verification, durable audit
storage, tenant identity, or a quality judgment. Those require stronger
receipts and an explicit backend or policy.

## Invariants

- Server-owned opaque IDs are storage keys.
- Caller IDs are bounded correlation metadata only.
- Redaction precedes persistence.
- Decision-time candidates are copied once and never recomputed on lookup.
- Every stream termination path closes upstream resources.
- Terminalization is idempotent.
- Unverifiable fields remain explicitly unobserved.
- Lookup is authorization-scoped. Scope mismatches are indistinguishable from
  missing records.

## Failure modes and mitigations

| Failure | Mitigation |
|---|---|
| Duplicate request IDs cross-link records | Opaque server IDs and per-record finalization keys |
| Prompt or secret leakage | Protocol-only feature extraction and pre-storage redaction |
| Stale eligibility changes the explanation | Immutable decision snapshot |
| Disconnect leaks an upstream stream | `finally` cleanup plus explicit close tests |
| HTTP success is mistaken for task success | Transport outcome is separate from verification and quality |
| Process restart loses evidence | Advertise process-local limitations; follow with a durable adapter |

## Verification evidence

Adopters should retain fixture-driven schema checks, lifecycle property tests,
resource-close assertions, authorization-scope tests, and a replay receipt
containing the code SHA and contract version. Admit positive learning labels
only from independently verified outcomes.
