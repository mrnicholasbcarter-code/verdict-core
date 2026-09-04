# Retention and Erasure Policy

## Retention window

Verdict retains operational receipts and memory records only for the period
needed by the associated workflow and evidence requirements. The default
GDPR-equivalent retention window is **30 days**.

## Erasure service level

An erasure request is honored **without undue delay, and in any case within 30
days** of the request. The automated release-gate test exercises this deadline
against synthetic data.

## Erasure procedure

1. Identify the requester's authorized storage scope.
2. Locate records in that scope using the durable record identifier or key.
3. Append a privacy-safe tombstone for each matching record. Tombstones contain
   only the target identifier and operation metadata; they do not copy deleted
   content.
4. Confirm normal retrieval, search, export, and replay paths no longer return
   the tombstoned content.
5. Preserve only the minimum audit metadata needed to prove that erasure was
   performed. The tombstone itself is subject to the same retention policy.

Erasure is scope-bound. A request cannot read or remove records belonging to a
different scope. If the target is absent, the operation is idempotent and does
not create a content-bearing record.

## Verification

The blocking test is
`tests/privacy/test_retention_erasure.py`. Run it with:

```bash
uv run pytest tests/privacy/test_retention_erasure.py -v
```

The test uses fabricated content and verifies that the record is unreachable
from ordinary retrieval after the 30-day deadline is applied.
