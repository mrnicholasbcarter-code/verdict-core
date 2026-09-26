# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- Untracked committed agent-workspace scaffolding (`.codex/`, `.cursorrules`, `.hermes.md`, `.serena/`) from the public repo and added it to `.gitignore`; canonical agent instructions remain in root `AGENTS.md`. `.prime/agent/**` is kept tracked (it is tested infrastructure — see `tests/test_prime_workflow.py`, `tests/test_prime_context.mjs`, `tests/test_prime_terminal.mjs`, `scripts/check_prime_discovery.mjs`, ADR-031).
- Retired `docs/archive/` (41 files); fixed inbound references in `docs/adr/README.md` and `SECURITY.md` to describe the retired content in prose instead of linking to it.
- `scripts/check_doc_links.py` no longer excludes `docs/archive/` by default (removed, since the directory is gone) and now validates in-file heading anchors (`path.md#anchor`) against GitHub heading-slug rules, not just file existence.
- Replaced internal `BOD-###` issue-tracker references across `docs/` and `tests/` with either the underlying ADR/concept they describe or plain prose; removed dead `linear.app` ticket links (kept the `BOD-###` id as plain text where useful). Renamed the 8 `tests/test_bod*.py` files to descriptive names (e.g. `test_bod196_intelligence_adapter_default_gate.py` -> `test_intelligence_adapter_default_gate.py`); no assertions or fixtures changed.
- Replaced `llm-gate.dev` schema `$id`/`schema_id` URLs (4 files) with `raw.githubusercontent.com` links to the actual file in this repo.
- Reworded recruiter/hiring-manager language under `specs/` to engineering-reviewer language; renamed `specs/455-recruiter-ready-readme/` to `specs/455-reader-ready-readme/`.
- restricted and trusted_upstream tasks now require an explicit restricted_data_routes allowlist; fail closed
- RoutingDecisionContract accepts an optional execution_envelope (validated with the ExecutionEnvelope rules; omitted when absent), matching the TypeScript contract.
- The API server now fails closed by default: unknown/error/timeout availability states are no longer admitted in the development profile. Opt in with `VERDICT_ALLOW_UNVERIFIED_DEV=1` (development profile only; see docs/CONFIGURATION.md).
- Startup now fails with `RuntimeError` when `OMNIROUTE_BASE_URL` (or the `LLMGATE_UPSTREAM_BASE_URL` OmniRoute fallback) is set to a value `OmniRouteHTTPTransport` rejects (bad scheme/path, non-loopback plain HTTP, non-allowlisted https host). `http://localhost:20128` is normalised to the loopback IP literal `http://127.0.0.1:20128` before validation, so the documented value still boots with the eligibility gate attached. A rejected `LLMGATE_UPSTREAM_BASE_URL` (used with no `OMNIROUTE_BASE_URL` set) is a direct-upstream proxy setting, not a misconfigured OmniRoute endpoint; it now logs a warning and boots without the availability cache instead of failing startup.
- `/v1/route/explain` eligibility now matches the live router's `dev_mode` (profile-derived), instead of a literal `dev_mode=True`.
- The in-memory receipts test literal (`PYTEST_CURRENT_TEST` sniff) was removed from production code. Tests must set `VERDICT_RECEIPTS_DB` explicitly (`tests/conftest.py` does this via an autouse fixture); authenticated mode with no `VERDICT_RECEIPTS_DB` configured now fails startup the same way in tests as in production.
- `verdict doctor` text mode now exits 1 when issues are found (previously exited 0). `--json` mode now exits 0 when healthy (previously non-zero) and adds a `warnings` key alongside `issues`.
- Documented (no behaviour change): `DEFAULT_PROFILE` stays `development` and fail-closed admission does not depend on it (see `LLMGATE_INTELLIGENCE_PROFILE` in docs/CONFIGURATION.md); `SubagentModelSelector` `dev_mode` never widens admission beyond the adapter's eligible set.
- Career/job-search-oriented docs and doc paths were removed or renamed; the security contact moved from email to GitHub Security Advisories. External deep links into the removed/renamed docs will break.

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
