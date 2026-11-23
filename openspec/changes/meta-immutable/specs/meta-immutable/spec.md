# Spec Delta

## ADDED Requirements

### Requirement: MetadataSnapshot shared fields are read-only

`MetadataSnapshot.sources` and `MetadataSnapshot.mapping` SHALL be read-only
after construction.  Any attempt to assign, insert, or delete a key SHALL raise
`TypeError`.

This requirement exists because `load_store()` returns a cached snapshot shared
by every caller in the process.  A mutable field in a shared object is a
write-after-cache-hit hazard that corrupts all subsequent callers silently.

#### Scenario: Mutating sources raises TypeError

- **GIVEN** a loaded `MetadataSnapshot`
- **WHEN** a caller executes `snapshot.sources["key"] = value`
- **THEN** `TypeError` is raised

#### Scenario: Mutating mapping raises TypeError

- **GIVEN** a loaded `MetadataSnapshot`
- **WHEN** a caller executes `snapshot.mapping["key"] = value`
- **THEN** `TypeError` is raised

#### Scenario: Read access is unaffected

- **GIVEN** a loaded `MetadataSnapshot`
- **WHEN** a caller reads `snapshot.sources.get("key")` or iterates `snapshot.mapping`
- **THEN** the operation succeeds and returns the expected value

### Requirement: load_store returns a cached identity-identical snapshot

When `load_store(path)` is called twice for the same file without any
intervening modification, both calls SHALL return the same Python object
(identity equality, `is`).

#### Scenario: Repeated load returns cached object

- **GIVEN** a metadata store file that has not been modified
- **WHEN** `load_store(path)` is called a second time for the same path
- **THEN** the returned snapshot is `is`-identical to the first
