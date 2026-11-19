## ADDED Requirements

### Requirement: A run can keep its gateway origin out of its dispatch-permit events

`DispatchAuthority.record_gateway_origin` SHALL default to true, and then each
`dispatch_permit` controller event SHALL record the permit scope's gateway
origin exactly as before. When it is false, each `dispatch_permit` event SHALL
record the literal string `redacted` as `gateway_origin`. `verdict orchestrate`
SHALL set it false only when `VERDICT_REDACT_GATEWAY_ORIGIN` is `1`. The
certification rehearsal script SHALL set that variable for every orchestrate run
and SHALL still refuse to upload a run directory in which any file contains the
gateway address or the gateway key. Redaction SHALL NOT change the permit check:
the executor's reported origin is compared with the real bound origin, and a
mismatched or missing origin is denied.

#### Scenario: Redacted rehearsal evidence

- **WHEN** a run with a non-loopback gateway origin is made with `record_gateway_origin` false
- **THEN** its `dispatch_permit` events and receipt do not contain that origin, the events record `redacted`, and its receipt verifies

#### Scenario: Default unchanged

- **WHEN** the same run is made without redaction
- **THEN** its `dispatch_permit` events record the origin as before

#### Scenario: Permit rule unchanged under redaction

- **WHEN** redaction is on and the executor reports another gateway's origin
- **THEN** the node is BLOCKED with `dispatch_denied: executor origin mismatch`, no permit is committed and the executor is not called

#### Scenario: Scan not weakened

- **WHEN** a rehearsal run file contains the gateway address or the gateway key
- **THEN** the script raises "Run evidence contains protected configuration; refusing upload" and copies nothing
