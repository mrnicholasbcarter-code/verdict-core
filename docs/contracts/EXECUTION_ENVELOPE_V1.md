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

Consumers MUST handle unknown fields gracefully according to the `additionalProperties: false` schema contract:

1. **Python (Core)**: `Contract.from_dict()` rejects unknown fields with `ContractValidationError`
2. **TypeScript/Node/Cockpit**: Consumers SHOULD validate against the schema and MAY reject unknown fields
3. **Test fixture**: `unknown-field.json` includes an additive field; expected verdict is `ACCEPT_IGNORING_UNKNOWN`

**Recommendation**: Consumers should log warnings for unknown fields but continue processing to enable forward compatibility.

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
| `unknown-field.json` | `ACCEPT_IGNORING_UNKNOWN` | Contains an unknown additive field |
| `null-defaults.json` | `ACCEPT_DEFAULTS` | Optional fields are null/absent |

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

1. **Eligibility Denial**: If `eligibility_decision.decision == "deny"` → `DENY`
2. **Digest Mismatch**: If `policy_digest != expected_policy_digest` → `DIGEST_MISMATCH`
3. **Expiry**: If `now >= execution_constraints.expires_at` → `EXPIRED`
4. **Otherwise**: `ACCEPT`

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
