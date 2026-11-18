# TypeScript contract parity

The `contracts/index.ts` boundary is a strict TypeScript consumer of the v1
contracts defined in [`schemas/contracts.v1.json`](../schemas/contracts.v1.json). <!-- claim:C-089 -->
It uses Zod for runtime validation and shares the canonical fixtures in
`tests/fixtures/` with the Python suite.

## Boundary rules

- `parseContract(name, value)` is the only supported v1 parsing entry point. <!-- claim:C-090 -->
- Unknown fields and secret-like keys fail closed with a categorized
  `ContractValidationError`. <!-- claim:C-091 -->
- `metadata`, `context`, signal values, and explanations remain open JSON
  objects only where the v1 schema permits them; safety fields and workflow
  actions remain strict.
- `parseLegacyContract` is an explicit migration entry point for the limited
  pre-v1 task and routing shapes. Legacy data is preserved under an explicit
  metadata boundary and is never silently treated as current policy. <!-- claim:C-092 -->
- `redactContractSecrets` is for diagnostic/event output. It redacts nested
  credential-like keys, credential-bearing URLs, and raw prompt/completion
  fields before they are retained. <!-- claim:C-093 -->

The TypeScript validator must remain no more permissive than the Python
runtime loader. The fixture tests compare semantic JSON serialization and
categorize the same canonical invalid cases. Changes to the JSON Schema or
fixtures must update both test suites in the same change. <!-- claim:C-094 -->

## Verification

```bash
npm ci
npm run build
npm test
```

The root CI workflow runs this clean-install Node parity suite alongside the
Python schema, test, lint, type, security, and package gates.
