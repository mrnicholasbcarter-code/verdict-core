# Phase 1 Data Model: Cross-Repository Security and Privacy Launch Gate

**Feature**: [spec.md](./spec.md) | **Research**: [research.md](./research.md) | **Date**: 2026-09-01

Seven entities carry this feature. Only two of them are new persistent records; the rest
are either derived at gate time or extensions of structures that already exist.

## Severity policy

The single stated threshold at or above which a finding blocks a release. Declared once
and read by every check.

| Field | Type | Rules |
|---|---|---|
| `blocking_severity` | enum: `low` \| `medium` \| `high` \| `critical` | Required. Ordered; comparison is `finding.severity >= blocking_severity`. |
| `policy_version` | string | Required. Increments when the threshold or the ordering changes. |

**Home**: the compatibility manifest (`verdict/compatibility_manifest.py`), inside the
hashed region so it cannot be re-signed at a weaker threshold.

**Validation**: an unrecognised severity name is not "unknown, therefore allow" — it is a
policy the reader cannot evaluate, and the gate refuses (FR-025).

**Relationships**: compared against every `Finding`; referenced by every `Exception`.

## Finding

An issue raised by a check.

| Field | Type | Rules |
|---|---|---|
| `id` | string | Required. Stable across runs for the same issue, so an exception can name it. |
| `severity` | severity enum | Required. Normalised into the policy's vocabulary at ingest, per source tool. |
| `source_check` | string | Required. Which check produced it. |
| `location` | string | Optional. File, package, or component. |
| `state` | derived enum: `blocking` \| `below-threshold` \| `excepted` | Not stored. Computed from the policy and the exception file at gate time. |

**Lifecycle**: raised by a check → compared to the policy → either blocks, falls below the
threshold, or matches a live exception. A finding is never edited to make it pass; only an
exception changes its effect.

**Note on normalisation**: the four scanners speak different severity vocabularies (see
research.md). Normalisation is the boundary validation this entity requires, and an
unmappable severity is treated as at or above the threshold, never below it.

## Exception

A recorded, expiring waiver for one finding.

| Field | Type | Rules |
|---|---|---|
| `finding_id` | string | Required. Must match a `Finding.id`. |
| `scope` | string | Required. What the waiver covers. |
| `rationale` | string | Required. Why the risk is accepted. |
| `approver` | string | Required. |
| `evidence` | string | Required. What was checked to justify acceptance. |
| `expires_on` | date | Required. Compared against the independently supplied build clock; no timestamp from the exception record is trusted as the current time. |
| `affected_repositories` | list of string | Required. Non-empty. |

**Home**: a tracked file in each repository, validated against a schema
(`contracts/security-exceptions.schema.json`).

**Field set is not invented**: it transcribes the constitution's governance clause, which
requires an exception to record scope, rationale, approver, evidence, expiry or follow-up,
and affected repositories.

**Validation and failure semantics** (FR-005d–f):

| Condition | Behaviour |
|---|---|
| File absent | No exceptions. Every finding at or above the threshold blocks. |
| File present, schema-invalid | Entries behave as **absent**, and the invalid file is itself reported. Never "allow all". |
| Entry expired | Behaves as absent. The finding blocks again. |
| Entry references no live finding | Reported as stale; does not block on its own. |

**Lifecycle**: added with an expiry → live until `expires_on` → expired, at which point the
finding it covered blocks again with no further action. Expiry is the mechanism; nobody
has to remember to remove it.

## Bill of materials

The component inventory for one published artifact.

| Field | Type | Rules |
|---|---|---|
| `artifact` | string | Required. The published distribution it describes. |
| `format` | string | CycloneDX (see research.md). |
| `components` | list | Each with name, version, and license where the source declares one. |
| `generated_at` | timestamp | Required. |

**Home**: generated into the evidence directory and attached to the release, one per
published artifact (Python distribution and each npm package).

**Relationships**: subject of a `Provenance attestation`; consumed by the dependency
checks that raise `Finding`s.

## Provenance attestation

The verifiable link from a published artifact back to the source revision and the run that
built it.

**Status**: this already exists for the Python distribution
(`actions/attest-build-provenance`, asserted by `tests/test_release_workflow.py`). This
feature extends the same guarantee to the bill of materials and to the npm packages rather
than introducing a new structure.

**Validation**: an artifact published without a matching attestation is a release failure,
not a warning.

## Retention rule

The category of stored data, its lifetime, and its disposition at end of life.

| Field | Type | Rules |
|---|---|---|
| `category` | string | Required. The class of stored data. |
| `lifetime` | duration | Required. |
| `disposition` | enum: `tombstoned` \| `redacted` \| `retained-as-reference` | Required. |
| `store` | string | Which store holds it. |

**Home**: the privacy policy document, which is the human-readable statement, with the
enforcing behaviour already present in `ReceiptStore.apply_retention` and
`adaptive_state._enforce_retention`.

**Amendment this feature makes**: the disposition `retained-as-reference` is stated
explicitly, because the evidence chain keeps non-reversible references past the lifetime
of the mutable data they were derived from.

## Non-reversible reference

A hash, identifier, or decision outcome the evidence chain retains after the data it was
derived from has been erased.

| Field | Type | Rules |
|---|---|---|
| `value` | string | Required. Must not be invertible to the erased content. |
| `derived_from_category` | string | Required. Which retention category it outlived. |

**Invariant**: appending an erasure record must leave the chain verifiable. This is the
property that makes erasure and append-only evidence compatible rather than contradictory,
and it is what SC-010 measures.

**Implementation note**: composed from the existing primitives —
`MemoryPlane.tombstone`, `ReceiptStore.tombstone`, and the redaction helpers in
`verdict/security.py`. No new erasure primitive is introduced; a deleter would contradict
ADR-017's ledger design.

## Entity relationships

```text
Severity policy ──(threshold)──> Finding ──(waived by)──> Exception
       │                            ▲                         │
       │                            │                         │
       │                     Bill of materials                 │
       │                            │                    (expires; then
  (carried in the             (attested by)               the finding
   compatibility               Provenance                  blocks again)
   manifest, hashed)           attestation

Retention rule ──(end of life)──> erasure ──(leaves behind)──> Non-reversible reference
                                                                       │
                                                            (chain must still verify)
```

## State transitions

Only two entities have meaningful state.

**Finding**: `raised` → `blocking` | `below-threshold` | `excepted`. Recomputed on every
run; nothing is stored, so a policy change re-evaluates history correctly.

**Exception**: `live` → `expired`. One-way. Renewal is a new entry with a new expiry and a
fresh approver, not an edit to the old one.
