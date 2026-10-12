# Spec Delta

## ADDED Requirements

### Requirement: Expected discovery failures settle the run
The orchestration CLI SHALL record an explicit BLOCKED terminal event when
live selector discovery rejects gateway authentication or permission before planning.
It SHALL preserve a credential-safe cause and return a nonzero exit status.

#### Scenario: Gateway rejects the configured key
- **WHEN** inventory discovery rejects the configured key with HTTP 401 or 403
- **THEN** the run contains a `run_finished` BLOCKED event with phase `startup`
- **AND** the external supervisor consumes this result without a restart
- **AND** no worker dispatch or COMPLETE receipt is claimed

#### Scenario: Unexpected implementation error
- **WHEN** selector construction raises an unexpected implementation exception
- **THEN** the exception remains a crash visible to existing supervisor recovery
