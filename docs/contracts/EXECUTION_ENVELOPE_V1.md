# ExecutionEnvelope Contract v1

## Overview

The `ExecutionEnvelope` is the canonical versioned execution contract from verdict-core to Node and Cockpit consumers. It contains all information needed for execution within Verdict-approved boundaries without requiring callbacks to Core for eligibility decisions.

## Contract Source

- **Python**: `verdict.contracts.ExecutionEnvelope` dataclass
- **JSON Schema**: `schemas/contracts.v1.json#/$defs/execution_envelope`
- **Fixtures**: `contracts/fixtures/execution-envelope/v1/`

## Schema Definition

```json
{
  "type": "object",
  "required": [
    "task_spec",
    "eligibility_decision",
    "policy_digest",
    "allowed_capabilities",
    "execution_constraints",
    "verification_requirements",
    "evidence_ids",
    "schema_version"
  ],
  "properties": {
    "task_spec": { "$ref": "#/$defs/task_spec" },
    "eligibility_decision": { "type": "object" },
    "policy_digest": { "type": "string", "pattern": "^[a-fA-F0-9]{64}$" },
    "allowed_capabilities": { "type": "array", "items": { "type": "string" } },
    "execution_constraints": { "type": "object" },
    "verification_requirements": { "$ref": "#/$defs/verification_plan" },
    "evidence_ids": { "type": "array", "items": { "type": "string" } },
    "routing_decision": { "oneOf": [{"type": "null"}, {"$ref": "#/$defs/routing_decision"}] },
    "created_at": { "oneOf": [{"type": "null"}, {"type": "string", "format": "date-time"}] },
    "schema_version": { "type": "string", "const": "1" }
  }
}
```

## Versioning Policy

### Schema Version

All ExecutionEnvelope instances carry `"schema_version": "1"`. This version is incremental and follows semantic versioning principles:

- **Breaking changes**: Major version increment (e.g., v1 → v2)
  - Removing required fields
  - Changing field types in incompatible ways
  - Changing validation semantics that would reject previously valid envelopes
  
- **Additive changes**: Minor version or patch (within v1.x)
  - Adding new optional fields
  - Relaxing validation constraints
  - Adding new enum values

### Unknown Field Handling

**v1 consumers MUST reject unknown fields. Additive fields require a schema_version bump.**

1. **Python (Core)**: `Contract.from_dict()` rejects unknown fields with `ContractValidationError`
2. **Verification**: `verify_execution_envelope()` returns `REJECT_UNKNOWN` for unknown fields
3. **Test fixture**: `unknown-field.json` includes an additive field; expected verdict is `REJECT_UNKNOWN`
4. **Schema**: `additionalProperties: false` on ExecutionEnvelope definition

This strict policy ensures that all parties agree on the exact contract shape. Future additive changes require incrementing `schema_version` from "1" to "2".

### Null vs Missing

Optional fields (`routing_decision`, `created_at`) may be:
- **Missing**: Field absent from JSON object
- **Null**: Field present with `null` value

Both representations are semantically equivalent. The `null-defaults.json` fixture demonstrates this.

### Version Negotiation

Consumers SHOULD:
1. Check `schema_version` field
2. Reject envelopes with unknown major versions
3. Accept envelopes with the same or older minor versions within the same major version

## Test Fixtures

The canonical fixtures are in `contracts/fixtures/execution-envelope/v1/`:

| Fixture | Expected Verdict | Description |
|---------|-----------------|-------------|
| `accepted.json` | `ACCEPT` | Valid envelope, all checks pass |
| `denied.json` | `DENY` | Eligibility decision denies execution |
| `expired.json` | `EXPIRED` | Envelope expired (expires_at in the past) |
| `wrong-digest.json` | `DIGEST_MISMATCH` | policy_digest does not match expected |
| `unknown-field.json` | `REJECT_UNKNOWN` | Contains an unknown field (v1 rejects) |
| `null-defaults.json` | `ACCEPT` | Optional fields (routing_decision, created_at) are null |

### Manifest

`manifest.json` contains:
- `contract_version`: Schema version (currently "1")
- `schema_id`: JSON Schema reference
- `fixtures`: Map of filename → {sha256, expected_verdict}

Consumers can pin to a specific manifest SHA-256 to ensure deterministic test behavior.

## Verification

Core provides `verdict.contracts.verify_execution_envelope()` to validate envelopes:

```python
from verdict.contracts import verify_execution_envelope, EnvelopeVerdict

verdict = verify_execution_envelope(
    envelope,
    now="2024-01-15T12:30:00Z",
    expected_policy_digest="a" * 64
)

assert verdict == EnvelopeVerdict.ACCEPT
```

### Verification Rules (Fail-Closed)

**All parameters are REQUIRED. Any malformed or skipped check returns a rejection verdict, never ACCEPT.**
**The verifier never raises on untrusted input; malformed data returns `REJECT_UNKNOWN`.**

1. **Schema Validation**: Parse with `ExecutionEnvelope.from_dict()` first
   - Unknown fields, wrong types, structural errors → `REJECT_UNKNOWN`
2. **Eligibility**: `eligibility_decision.admitted` must be `True` AND no contradictory signals
   - `admitted` is not `True` → `DENY`
   - `denied` is truthy → `DENY` (even if `admitted=True`)
   - `decision` present and not `"accept"` → `DENY` (even if `admitted=True`)
   - Contradictory signals fail closed
3. **Digest Mismatch**: Missing, empty, or wrong `policy_digest` → `DIGEST_MISMATCH`
4. **Expiry** (bounded lifetime REQUIRED):
   - **Missing `execution_constraints.expires_at`** → `EXPIRED`
   - Unparseable `now` or `expires_at` → `EXPIRED`
   - Timezone-naive timestamps → `EXPIRED`
   - `now >= expires_at` → `EXPIRED`
5. **All checks passed**: `ACCEPT`

**Parameters:**
- `now`: ISO 8601 timestamp (REQUIRED, must include timezone)
- `expected_policy_digest`: SHA-256 hex digest (REQUIRED)

**Why expires_at is REQUIRED**: Envelopes without expiry can be replayed forever. The fail-closed policy
requires a bounded lifetime. Core producers MUST set `execution_constraints.expires_at`. Consumers
MUST verify it.

**Lifetime guidance**: Producers SHOULD keep lifetimes short; consumers MAY enforce a maximum lifetime
(expires_at - created_at) appropriate to their security requirements.

## Consumer Integration

### Node/Cockpit (TypeScript)

```typescript
import { ExecutionEnvelope } from '@bodanglin/verdict-contracts';
import manifest from '@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/manifest.json';

// Load and validate fixture
const acceptedFixture = require('@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/accepted.json');
// Validate against schema
// ...

// Pin to manifest
const expectedSha = manifest.fixtures['accepted.json'].sha256;
```

### Python (importlib.resources)

```python
from importlib.resources import files
import json

fixtures_dir = files('verdict').joinpath('../contracts/fixtures/execution-envelope/v1')
manifest = json.loads((fixtures_dir / 'manifest.json').read_text())
```

Or directly from repository:
```python
from pathlib import Path
repo_root = Path(__file__).parent.parent
fixtures = repo_root / "contracts/fixtures/execution-envelope/v1"
```

## Relation to BOD-12 Parity

This work supersedes [BOD-12](https://linear.app/bodanglin/issue/BOD-12) historical TypeScript/Python parity with a **producer/consumer contract model**:

- **verdict-core** (this repo): Canonical producer of ExecutionEnvelope
- **Node/Cockpit**: Consumers that validate against the schema and fixtures

The Python `ExecutionEnvelope` dataclass is now the single source of truth, and the JSON schema + fixtures are the cross-language contract.

## Related Work

- **BOD-85**: Node adoption of canonical fixtures (separate story)
- **BOD-14**: Cockpit adoption of canonical fixtures (separate story)
- **BOD-193**: Earlier Ruflo references removed; Core → Node/Cockpit is the current model
