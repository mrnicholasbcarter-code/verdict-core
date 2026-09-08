# E2-S1 Canonical Context-Pack Contract

## Goal

Define one deterministic, provenance-aware context-pack contract that can be
validated by both Python and TypeScript consumers before routing.

## User scenarios

- A compiler receives active, superseded, disputed, stale, and unavailable
  context units and emits a bounded pack whose decisions explain every
  omission.
- Two processes compile the same serialized logical inputs and produce the
  same canonical JSON and digest.
- A consumer validates a pack containing source identity, observation and
  retrieval timestamps, confidence, status, and content provenance.
- A secret-bearing unit is rejected from model-visible context while its
  exclusion remains auditable.

## Functional requirements

1. Context units MUST carry source identity, source digest, revision,
   observed-at, retrieved-at, confidence, status, and content.
2. Status handling MUST be deterministic: active and observed units outrank
   stale units; superseded and disputed units MUST NOT be selected over active
   evidence, and unavailable or missing units MUST be omitted with reason
   codes.
3. Every excluded or transformed unit MUST have an auditable decision with a
   stable reason code.
4. Compilation MUST enforce the input token budget and preserve required
   provenance metadata.
5. Canonical serialization and digest generation MUST be stable for identical
   serialized inputs across processes.
6. Python schemas and TypeScript contracts MUST accept and reject the same
   context-pack shapes, including strict unknown-field handling.
7. Secret-bearing content MUST never enter the compiled prompt.

## Success criteria

- Focused Python tests cover active/superseded, disputed, stale, unavailable,
  malformed, budget-boundary, determinism, and secret-redaction cases.
- TypeScript contract tests parse valid packs and reject malformed or
  secret-bearing packs.
- Both schema copies validate the same representative fixture.

## Scope

Included: `verdict/context_pack.py`, context-pack schemas, the TypeScript
contract package, and focused tests. Excluded: provider retrieval and routing
behavior.

