# @bodanglin/verdict-contracts

Canonical Zod schemas and TypeScript types for Verdict v1 contracts.

## ExecutionEnvelope v1 Contract

The `ExecutionEnvelope` is Verdict's canonical execution contract from verdict-core to Node and Cockpit consumers. Each envelope contains all information needed for execution within Verdict-approved boundaries without requiring callbacks to Core for eligibility decisions. Fields include:
- `task_spec`: Task description and metadata
- `eligibility_decision`: Admission status
- `routing_decision`: Optional routing metadata
- `policy_digest`: SHA-256 hex digest (64 lowercase hex characters, no prefix)
- `allowed_capabilities`: Permitted capability identifiers
- `evidence_ids`: Referenced evidence identifiers
- `execution_constraints`: Hard constraints including `expires_at` (bounded lifetime)
- `verification_requirements`: Required verification checks and plans
- `created_at`: Optional envelope creation timestamp
- `schema_version`: Contract version identifier (currently "1")

Verdict verifiers parse the envelope, validate the schema (rejecting unknown fields), check eligibility, verify the policy digest matches the expected policy, and enforce time-bounded execution via `expires_at`. If all checks pass, the verifier returns `ACCEPT`; otherwise, it returns `DENY`, `EXPIRED`, `DIGEST_MISMATCH`, or `REJECT_UNKNOWN`.

Full semantics: [docs/contracts/EXECUTION_ENVELOPE_V1.md](https://github.com/mrnicholasbcarter-code/verdict-core/blob/main/docs/contracts/EXECUTION_ENVELOPE_V1.md)

## Fixtures: Canonical Test Vectors

This package ships **sha256-pinned test fixtures** for ExecutionEnvelope v1:

- **`fixtures/execution-envelope/v1/`**: Six canonical test cases (`accepted.json`, `denied.json`, `expired.json`, `null-defaults.json`, `unknown-field.json`, `wrong-digest.json`) with expected verdicts and sha256 digests in `manifest.json`.
- **`fixtures/execution-envelope/v1-mutations/`**: A mutation corpus (`cases.json`) for Python/Zod validation parity, sha256-pinned by `manifest.json`.

**Loading a fixture:**

```typescript
import manifest from '@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/manifest.json' with { type: 'json' };

// Or with createRequire (for older Node or CommonJS):
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const manifest = require('@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/manifest.json');
```

The manifest lists each fixture file, its expected verdict, and its sha256 digest. Verify fixture integrity by hashing the file and comparing to the manifest.

## Versioning

This package follows **0.x.y** versioning: minor bumps (0.2→0.3) **may break** backward compatibility. Pin your dependency or test after updates.

## Node Engines

Requires **Node ≥18**.

## License

MIT
