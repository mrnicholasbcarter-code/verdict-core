# @bodanglin/verdict-contracts

Canonical Zod schemas and TypeScript types for Verdict v1 contracts.

## ExecutionEnvelope v1 Contract

The `ExecutionEnvelope` is Verdict's core contract for policy-gated LLM execution. Each envelope carries:
- A request payload (model, messages, tools, constraints)
- A policy SHA-256 digest commitment
- An optional signature for audit trails

Verdict verifiers parse the envelope, enforce execution constraints (e.g., `expires_at` timestamps), verify the policy digest matches the installed policy, and strictly reject unknown fields to prevent contract drift. If all checks pass, the verifier returns `ACCEPT` and unpacks the request for execution; otherwise, it returns `DENY`, `EXPIRED`, `DIGEST_MISMATCH`, or `REJECT_UNKNOWN`.

Full semantics: [docs/contracts/EXECUTION_ENVELOPE_V1.md](https://github.com/mrnicholasbcarter-code/verdict-core/blob/main/docs/contracts/EXECUTION_ENVELOPE_V1.md)

## Fixtures: Canonical Test Vectors

This package ships **sha256-pinned test fixtures** for ExecutionEnvelope v1:

- **`fixtures/execution-envelope/v1/`**: Six canonical test cases (`accepted.json`, `denied.json`, `expired.json`, `null-defaults.json`, `unknown-field.json`, `wrong-digest.json`) with expected verdicts and sha256 digests in `manifest.json`.
- **`fixtures/execution-envelope/v1-mutations/`**: A mutation corpus (`cases.json`) for Python/Zod validation parity, sha256-pinned by `manifest.json`.

**Loading a fixture:**

```typescript
import manifest from '@bodanglin/verdict-contracts/fixtures/execution-envelope/v1/manifest.json' assert { type: 'json' };
// or with createRequire for older Node
```

The manifest lists each fixture file, its expected verdict, and its sha256 digest. Verify fixture integrity by hashing the file and comparing to the manifest.

## Versioning

This package follows **0.x.y** versioning: minor bumps (0.2→0.3) **may break** backward compatibility. Pin your dependency or test after updates.

## Node Engines

Requires **Node ≥18**.

## License

MIT
