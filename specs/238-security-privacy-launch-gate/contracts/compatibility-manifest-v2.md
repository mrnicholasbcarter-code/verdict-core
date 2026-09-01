# Contract: compatibility manifest, schema version 2

**Owner**: `verdict-core` (`verdict/compatibility_manifest.py`)
**Consumers**: `verdict-core` CI, `verdict-node` CI
**Supersedes**: schema version 1, as shipped by ADR-024

## What changes

Version 1 carries `schema_version`, `manifest_hash`, and `contracts`. Version 2 adds the
declared security policy, inside the hashed region.

```json
{
  "schema_version": "2",
  "manifest_hash": "<hex>",
  "contracts": { "<name>": "<hash>" },
  "security_policy": {
    "blocking_severity": "high",
    "policy_version": "1"
  }
}
```

## Why the policy is inside the hashed region

`manifest_hash` currently covers `contracts` only. If the policy sat outside it, a manifest
could be re-emitted with a weaker threshold and still present a matching hash. Version 2
extends the hashed region to cover the policy, so the threshold cannot be changed without
changing the hash.

This is a hash-input change. Every version-1 hash is therefore invalid under version 2 by
construction, which is correct: they described a manifest with no policy in it.

## Fail-closed behaviour comes free

`CompatibilityManifest.__post_init__` raises unless `schema_version == "1"`. A consumer
still running version-1 code rejects a version-2 manifest outright rather than reading it
partially and proceeding without a policy.

This satisfies FR-025 with the invariant that already exists. **No new rejection branch
should be added for the "old reader, new manifest" case** — writing one would create a
second, weaker path around a guard that already fails closed.

The case that *does* need new code is the reverse: a version-2 reader encountering a
`blocking_severity` it does not recognise. That is not "unknown, therefore allow"; the
reader cannot evaluate the policy and must refuse.

## Required behaviour

| Situation | Required outcome |
|---|---|
| Version-1 reader, version-2 manifest | Reject (existing guard). |
| Version-2 reader, version-1 manifest | Reject. A manifest with no policy cannot gate a release. |
| Version-2 reader, unrecognised `blocking_severity` | Reject. Do not fall back to a default. |
| Version-2 reader, `security_policy` absent | Reject. Absence is not permission. |
| Hash mismatch | Reject, as in version 1. |

## Cross-repository parity

No TypeScript mirror of this structure exists today in `contracts/` or
`verdict/client-sdk/`. Version 2 requires one, because `verdict-node` must read the policy
to enforce the same threshold.

Ordering is fixed by Constitution III: `verdict-core` lands and publishes first;
`verdict-node` follows in its own pull request. The two must not share a commit.

Parity surfaces to keep in step:

1. The Python dataclass and its serialisation (`verdict/compatibility_manifest.py`).
2. The manifest JSON schema, wherever the repository declares it.
3. The TypeScript reader in `verdict-node`.

Adding a field to fewer than all of them breaks the round-trip and parity tests.

## Command surface

`verdict compat manifest` emits the version-2 manifest. `verdict compat check` verifies it
and fails closed. Both already exist (`verdict/cli.py:2553-2561`); this feature changes what
they carry and, for the first time, wires them into CI (FR-024).
