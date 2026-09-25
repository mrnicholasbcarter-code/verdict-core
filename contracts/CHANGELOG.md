# Changelog

All notable changes to `@bodanglin/verdict-contracts` will be documented in this file.

## [0.3.0] - 2026-09-25

### Added
- ExecutionEnvelope v1 schema with full Zod/TypeScript definitions
- Verifier logic parity with Python implementation
- `execution_constraints.expires_at` field for time-bound execution
- Strict unknown-field rejection to prevent contract drift
- Canonical test fixtures (`fixtures/execution-envelope/v1/`) with sha256-pinned manifests
- Mutation corpus (`fixtures/execution-envelope/v1-mutations/`) for validation parity testing
- Package exports for `./fixtures/*` and `./package.json` to enable direct fixture imports

### Changed
- Package version bumped to 0.3.0

## [0.2.0] - 2026-08-22

Receipt parity schemas.

## [0.1.0] - 2026-07-26

Initial public release.
