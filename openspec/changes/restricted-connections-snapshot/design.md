# restricted-connections-snapshot design

## Context

Certification workflows need connection evidence (which routes are live, their
quota state, cooldown windows) to decide which routes to certify. Sending an
admin-scoped key through a public CI tunnel exposes credentials. Inference-scoped
keys can read models but cannot call admin provider-inventory endpoints.

A sanitised snapshot — captured through loopback-only gateway administration,
bound to a SHA-256 digest and limited to a six-hour freshness window — lets the
CI workflow supply connection evidence without exposing credentials.

## Goals / Non-Goals

**Goals:**
- Capture sanitised connection evidence through loopback-only gateway
  administration and write it atomically to a versioned file.
- Validate the snapshot on every read: schema, freshness, payload digest,
  and sanitised evidence fields.
- Require a masked protected snapshot secret for certification rehearsals.
- Keep admission and ranking authority unchanged.

**Non-Goals:**
- Changing the connections API or any management endpoint behaviour.
- Providing a fallback when the snapshot is absent or invalid; the correct
  behaviour is fail closed.
- Making the digest a signature or admission authority.

## Decisions

Loopback-only capture: the snapshot is written only over the loopback
interface, so the admin key never leaves the local machine.

Private atomic writes: the file is written to a temporary path and renamed
atomically to prevent partial reads.

SHA-256 digest: bound to the payload so corruption or tampering is detected
on every read without requiring a signing key.

Six-hour freshness: matches the existing orchestrate freshness rule and is
short enough to prevent stale evidence from reaching certification.

Closed vocabulary for evidence: sanitised evidence fields are defined in the
schema; unknown fields are dropped on write.

## Risks / Trade-offs

Fail-closed: an absent, stale or tampered snapshot fails certification. This
is intentional; guessing at capacity would be worse than a hard failure.

The 6-hour window is a fixed policy; workflows that cannot refresh within that
window would need a different arrangement.

## Security / trust implications

KNOWN: the snapshot is captured over loopback only; the SHA-256 digest detects
corruption or replacement; the schema strips fields that could carry credentials.

INFERRED: masking the protected snapshot secret in the rehearsal workflow
prevents the raw value from appearing in CI logs or artifacts.

## Routing / context / memory implications

No impact on routing decisions beyond the capacity inference that already ran
from management reads.

## Migration / rollback

The snapshot file is opt-in: existing workflows that do not set
`VERDICT_CONNECTIONS_SNAPSHOT` are unaffected. Rolling back means removing
the snapshot capture step and the environment variable.

## ADR impact

No ADR changes required. The sanitised-evidence pattern is consistent with
existing security design decisions.
