# Implementation plan

1. Extend `ContextUnit` with retrieval timestamp and explicit lifecycle status,
   validate and serialize both fields, and keep legacy slot conversion
   backwards-compatible through defaults.
2. Add deterministic status precedence and fail-closed omission decisions to
   `ContextPackCompiler`, including stable pack creation metadata.
3. Update both JSON schema copies and add the context-pack zod contract and
   registry aliases in `contracts/src/index.ts`.
4. Add table-driven Python and TypeScript coverage for lifecycle status,
   missing/unavailable sources, determinism, redaction, and schema parity.
5. Run focused tests, type checks, lint, and the repository contract parity
   validation.

