## ADDED Requirements

### Requirement: Native orchestration outcomes feed session evidence
Verdict SHALL append one controller-observed `SessionOutcome` for each dispatched worker terminal attempt and each reviewer attempt. Records SHALL identify route, reported model, accepted/failed result, normalized session failure class, observed failure category, token counts when reported, and wall time when reported.

#### Scenario: A worker attempt settles
- **WHEN** a real worker attempt reaches terminal success or failure
- **THEN** the default session ledger contains exactly one corresponding record
- **AND** eligibility exposes its resulting session statistics

#### Scenario: An independent review settles
- **WHEN** a reviewer adapter reports an attempt
- **THEN** the same ledger contains a review-attempt outcome

### Requirement: Improvement lessons are gated cited facts
Verdict SHALL write each measured non-MET improvement lesson through `MemoryGate` in namespace `lessons`, only after its run receipt verifies.

#### Scenario: A measured cycle learns
- **WHEN** an improve cycle has a valid receipt and classifies a blocker
- **THEN** MemoryPlane contains one authority-verified `lessons` record
- **AND** provenance cites the cycle, run, receipt path, and receipt SHA-256
- **AND** a later BOD-306 worker recall may cite the record

#### Scenario: Evidence storage fails
- **WHEN** either advisory writer cannot persist
- **THEN** the authoritative orchestration result remains unchanged
- **AND** the cycle log reports whether lesson memory was written
