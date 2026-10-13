# Spec Delta: Restricted gateway connections evidence

## ADDED Requirements

### Requirement: Connection evidence SHALL be captured on loopback only
`verdict gateway connections-snapshot --out PATH` SHALL read provider
connections with the admin key on a loopback gateway only, refuse proxy
environment variables, use no redirects and write a sanitized, versioned
snapshot privately and atomically. The snapshot SHALL contain fixed categories
and opaque ids only.

#### Scenario: Proxy variables are set
- **WHEN** `HTTPS_PROXY` is set during capture
- **THEN** the capture refuses and writes nothing

### Requirement: Connections SHALL be read from a valid snapshot without HTTP fallback
When `VERDICT_CONNECTIONS_SNAPSHOT` is set, the connections reader SHALL use the
snapshot and SHALL NOT call `/api/providers`. The snapshot SHALL be rejected
when its schema, SHA-256 payload digest, or age (more than six hours, or in the
future) is invalid. The digest detects corruption only; it is not a signature
or admission authority. Unknown evidence SHALL stay unknown.

#### Scenario: Stale snapshot
- **WHEN** the snapshot is seven hours old
- **THEN** the reader rejects it and makes no HTTP request

#### Scenario: Digest mismatch
- **WHEN** one byte of the snapshot payload changes
- **THEN** the reader rejects it with a digest error

### Requirement: Certification SHALL require a masked snapshot secret
Certification rehearsals with a restricted key SHALL require the protected
snapshot secret, mask it, write it with mode 0600 under the runner temp
directory and pass only its path to child processes.

#### Scenario: Secret missing
- **WHEN** a restricted-key rehearsal runs without the snapshot secret
- **THEN** the rehearsal fails before any provider call
