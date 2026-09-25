# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- restricted and trusted_upstream tasks now require an explicit restricted_data_routes allowlist; fail closed

## [0.3.0] - 2026-09-25

### Added
- ExecutionEnvelope v1 contract with Zod/TypeScript/Python parity (#602, #603, #604)
- Canonical test fixtures with sha256-pinned manifests and mutation corpus for validation parity
- Release version certifier and governance enforcement (#605, #612, #613)
- Strict typing enforcement and coverage floor requirements (#606)
- OpenSpec lifecycle management (#607)
- Codiv candidate implementation (#608)
- OpenJev SHADOW integration (#609, #610)
- Route identity tracking in execution receipts (#611)
- Core model metadata store (BOD-108): models.dev + LiteLLM fetchers, explicit OmniRoute map, `verdict metadata refresh|show|lookup`
- Architect-locked Codex harness CLI: `verdict harness codex enable|disable|status`

### Changed
- Package versions synchronized to 0.3.0 across verdict-core, @bodanglin/verdict-contracts, and @bodanglin/verdict-client

## [0.2.0] - 2026-08-22

### Added
- Credential-free accepted and denied flagship quickstart journey.
- Attested, immutable Python and npm release candidate workflow.

### Changed
- Corrected client package repository metadata and package-content verification.
- Consolidated npm, PyPI, and GitHub release publication behind one tag workflow.

### Added
- Comprehensive test suites with 100% coverage
- GitHub Actions CI/CD pipeline with CodeQL security scanning
- Automated dependency updates via Dependabot
- CodeQL security analysis
- Automated linting and formatting

## [0.1.0] - 2024-01-15

### Added
- Initial release
