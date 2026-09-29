# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-09-29

### Added
- `verdict demo`: credential-free offline scenario that drives scripted workers through a full run and exits 0 with all routing and failover claims VERIFIED (#737)
- `verdict trace`: display per-event execution trace for a recorded run, with optional routing and context detail (#737)
- `verdict routing`: CLI view of the routing decision and candidate ranking for a run (#734)
- `verdict context`: CLI view of context provenance and budget breakdown for a run (#734)
- `verdict run-receipt`: verify and display the integrity-bound receipt for a completed run (#727)
- `verdict --version`: prints the installed version and exits 0
- Cockpit: interactive TUI navigation (`verdict watch`) with deep links to routing, context and run-controls views (#736, #718)
- Cockpit: routing explorer TUI and text renderer showing eligibility, ranking and selection per node (#731, #716)
- Cockpit: context budget provenance panel showing per-route context assembly (#728, #715)
- Cockpit: run controls — cancel run/node, retry via RecoveryBudget (#713)
- Cockpit: candidate panel follows each route through failover (#657, #656, #658)
- Shared action layer with CLI/TUI parity matrix; all cockpit actions reachable from both surfaces (#708, #736)
- Visual system: unified design tokens, glyph registry and motion policy for TUI and charts (#710, #724, #723, #735)
- Charts redesign: new admission, recovery and cost chart renderers (#723)
- Routing explorer projection: real rank components and selection explanation from recorded eligibility evidence (#714)
- Per-run receipt: integrity verification, barrier and research-node commits recorded (#705, #727)
- Trace evidence events: per-route eligibility verdicts, hydrated sources and attempt usage in run events (#711)
- OpenTelemetry tracing (`verdict[tracing]`, opt-in via `VERDICT_TRACING=1`): spans exported to OTLP endpoints; prompts and secrets are not attached (#690)
- Supervisor: failure-directed context rehydration before escalation (#647)
- Supervisor: repack smaller context after overflow before retry (#644)
- Supervisor: skip too-small context windows after overflow (#640)
- Supervisor: suppress duplicate context units (#641)
- Supervisor: context overflow classified distinctly from capability failures; relay threads upstream body into classification (#666, #671)
- Supervisor: per-attempt token usage recorded in events and receipts (#669, #685)
- Supervisor: per-story state isolation for multi-story supervisors (#691)
- Supervisor: GlobalConcurrencyGovernor limits concurrent orchestration runs (#673)
- Supervisor: merge-guard serialises concurrent merges (#680)
- Supervisor: real cross-process concurrency for multi-story supervisor (#679)
- Supervisor: concurrency governor flag (#678)
- Supervisor: ready_gate READY/EXCLUDED/WAIT admission decision (#672)
- Supervisor: story footprint with collision detection (#674)
- Orchestration: reviewer idle/no-progress timeout (#648)
- Orchestration: reviewer revalidation before OCR launch (#634)
- Orchestration: controller capability floor; independent role assignment (#632)
- Orchestration: cheapest sufficient model wins; insufficient candidates dropped (#629)
- Orchestration: unknown model capability stays explicit rather than assumed (#630)
- Orchestration: capability floor on Prime worker selection (#639)
- Orchestration: frontier guard by capability tier, metered-only (#645)
- Orchestration: failover survives real provider failures (#628, #626)
- Orchestration: controller capability tier (#632)
- Verdict harness prime visibility drift diagnostic (`verdict harness prime visibility`) (#651)
- `verdict doctor` reports shared-memory auth failure with code `auth_failed` (#703)
- OpenJev SHADOW collector for calibration, response self-consistency validation, and calibration harness (#642, #663, #637)
- Calibration: role field, per-category cost tracking, per-role reports; real per-attempt token usage (#664, #685)
- install.sh: SHA-256 pinned integrity check for wheel and sdist with PyPI API fallback for non-default versions (#696)
- AI-memory experimental adapter (proof-of-concept, opt-in) (#709)
- Catalog truth: capacity classification and backend pool identity (#741)

### Changed
- Live cost benchmark with real models (opt-in, `VERDICT_LIVE_SMOKE=1`): the offline catalog router selected the baseline model for every task; no routing saving was measured. Bench script at `scripts/live_savings_bench.py`; proof at `docs/proof/live-savings-2026-09-28/` (#702)
- Provider/gateway bootstrap unified in one offline contract shared by CLI, `verdict serve`, supervisor and library `Gate`; URL userinfo redacted in all diagnostics; `profile: production` refuses default provider sets (#pre-existing in [Unreleased])
- OpenJev influence: context-length failure class; ranking receipts on InfluenceRecord (#670, #667)
- `verdict doctor` exits 1 when issues are found (text mode); `--json` mode exits 0 when healthy (#pre-existing)
- `verdict harness prime sync-models` writes models.json with owner-only 0600 permissions (#pre-existing)
- Parity matrix expanded with harness lifecycle facets (#689)
- Prime supervisor sync no-op when models unchanged; backup pruning; optional timer (#646)
- TUI replay mode for recorded demos (#701)
- Orchestration outcome records from run receipts (#688)
- Architecture diagrams added under `diagrams/`; visual documentation updated (#725, #693)

### Fixed
- Run receipts: worker-only fault keys and no-change nodes no longer appear on unrelated receipt entries (#727)
- Orchestration: receipt integrity barrier, research node commits (#705)
- Orchestration: catch verify-command FileNotFoundError; resolve Python interpreter (#692)
- Orchestration: usage collection from message_end; persist in attempt JSON (#683)
- Orchestration: sum usage over all assistant turns (#684)
- Orchestration: accept Prime usage.cost dict in PrimeHeadlessExecutor (#681)
- Orchestration: post-probe fields added to eligibility event (#682)
- Orchestration: 403 is provider-scoped on worker path (#654)
- Orchestration: `supervise` passes watched `--runs-dir` (#659)
- Orchestration: unknown-health routes enter worker pre-probe pool (#636)
- Scoped eligibility never shrinks the Prime registry (#635)
- Prime: classify kiro context overflow so Prime compacts instead of retrying (#707)
- `verdict route` / API relay: thread upstream body into context-length classification (#671)
- Benchmarking: timing-flaky threshold assertions stabilised (#699, #661)
- Probe budget timing flake fixed (#700)
- CodeQL: clear-text-logging alert on provider env-var names resolved; provider detection logging redacted (#738, #733)
- TUI recordings: pty size, timing and capture validation corrected (#726)
- `follow()` is non-interactive unless the caller requests it (#719)

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
