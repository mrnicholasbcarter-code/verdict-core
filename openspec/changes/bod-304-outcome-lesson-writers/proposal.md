# Proposal: BOD-304 native outcome and lesson writers

## Why

The session ledger feeds eligibility evidence but production orchestration never writes it. Improvement lessons exist only in a cycle journal and therefore cannot enter BOD-306 worker recall. Ranking policy is owned by the separate eligibility-finish change.

## What Changes

- Every real worker terminal attempt and every independent-review attempt appends controller-observed route, reported model, result, failure class, token usage, and wall time to `SessionLedger`.
- A measured non-MET improve cycle writes its selected bounded lesson through `MemoryGate` in namespace `lessons`, cited to the digest-verified run receipt.
- BOD-306 fact recall includes the gated `lessons` namespace.

## Impact

The ledger remains advisory and append-only. A writer failure cannot change a run result. Existing session records remain readable because added metrics are optional.
