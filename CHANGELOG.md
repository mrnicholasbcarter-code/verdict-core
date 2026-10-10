# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Planner selection no longer ends with "none eligible" when cold catalog routes would use the whole confirmation budget: fresh cache-positive routes are confirmed first, one slot is reserved for a fresh healthy subscription fallback, and routes refreshed during selection are used in the same selection. Found by live certification rehearsals.
- The health cache default path follows `VERDICT_HOME` like every other store. **Behaviour change:** with `VERDICT_HOME` set, an older `~/.verdict/health-cache.json` is no longer read; route health rebuilds on the next refresh or prove cycle, and session evidence can be re-imported with `verdict prove-at-rest import-sessions`.
- With `--state-file` or `--inject`, `verdict orchestrate` reads `health-cache.json` next to the state file (the file its refresh job writes), not the default cache. Isolated and chaos runs therefore start from their own evidence.
- Invalid planner output is saved (redacted, at most 64 KiB) in the run directory; redaction now also covers bare `Bearer` tokens and unlabelled provider key prefixes.

## [0.5.0] - 2026-10-09

### Added
- A verified-model view projects per-route VERIFIED/UNVERIFIED/FAILED status from local health-cache evidence, with a bounded refresh that can spend prepaid quota only on exact, consented ids (#791, #792).
- A Prime picker (`verdict harness prime select|restore`) previews and applies an exact `/enabledModels` scope under digest and proof-deadline guards, with byte-for-byte backup/restore (#793).
- A Claude Code model-compatibility report, read-only, with no apply path (#793).
- Context-aware local autocomplete for the command prompt, sanitized against the frozen last-published projection snapshot (#793).

### Fixed
- TUI eligibility gateway origin and failure exit codes (#789).
- Real-registry and probe-category data: `agentic_fail` and `http_error` are accepted as known diagnostic categories, and display-unsafe registry rows no longer abort the Prime registry or binding (#796).
- Prime picker path safety: an absent optional project settings file under a group/other-writable `.prime/` parent (a realistic 0775-umask checkout) no longer refuses `apply`; a present project file still gets full safety checks. Path-safety refusals (`unsafe_directory`, `unsafe_file`, `unsafe_path`) now report a home-relative location and a `chmod go-w` hint instead of a bare, non-actionable message (this branch).
- Repo-wide `actionlint` findings on `main`; CI gained `actionlint` and `vulture` (#794, #795).

### Dependencies
- `source-map-js` 1.2.2 for the known 1.2.1 advisory (indirect, via `postcss`) (#783).
- `http-cache-semantics` stays at 4.2.0 (already the newest published release); no patched version exists yet for its open advisory, so it is tracked, not hacked around.

### Known limitations
- Certification is not yet attested: the OIDC rehearsal attester step is pending.
- Worker selection does not read the verified-model health cache yet (BOD-300); verified status is informational, not a routing input.
- The Prime picker's proof deadline gives roughly 74 seconds from the apply confirmation ("Yes") to the guarded rename; a slower confirm flow needs a fresh preview.
- A `Retry-After` spec/code mismatch is tracked and not yet fixed in this release.
- `install.sh` keeps installing 0.4.2 until 0.5.0 is published to PyPI; its pin is updated post-publish with real digests.

## [0.4.2] - 2026-10-01

### Added
- Release workflow is set to build and smoke-test the container image without network access before publication, push the version tag to GHCR, attest its push digest, and only then update `:latest` (#774). This describes the workflow; publication still requires the approved tag run.
- A devcontainer runs the credential-free demo on attach. The SSH feature makes Codespaces verifiable with `gh codespace ssh` (#774, #775).
- Clean-container CI captures fresh install transcripts for `pipx`, `uvx`, and the integrity-verified install script (#770).

### Fixed
- Container default command and writable data directory; CI checks default demo, server startup, and refusal paths (#773).
- Scoped controller route selection and receipt handling (#772).
- Live-demo wording, README evidence links, and mixed-harness live proof (#768, #769, #771).

## [0.4.1] - 2026-09-30

### Changed
- Bare `verdict` on a terminal opens a command prompt (`verdict ›`) instead of the numbered selector. It has history, Tab completion and `/` commands, and it shows results inline. Free text is treated as a goal. It launches `verdict orchestrate` only after an explicit `y` (#761).
- Startup motion: the wordmark reveals, a "Checking gateway reachability" task is shown, and then REACHABLE or UNREACHABLE appears. Motion is off for CI, `NO_COLOR`, `TERM=dumb` and `VERDICT_NO_ANIMATION` (#761).
- README: the first run is `pip install verdict-core` then `verdict demo`. OmniRoute and `provider/model` are defined at first use. The capacity-order section matches the role-aware ladder (#759, #760).
- The README walkthrough recording (`docs/assets/demo-tui.*`) is re-recorded. It opens on the new home screen ("Type a goal, or / for commands") (#762, #763).
- The gallery home image uses the same offline-scenario label as the rest of the gallery (#764).

### Fixed
- `install.sh` pins a published version with PyPI digests. A fresh install no longer exits 1 from its cleanup trap, and it no longer fails `verdict check` before any config exists (#759).
- Prompt history is capped at 500 lines and is owner-only (0700 directory, 0600 file) (#761).
- The asciicast recorder keeps multi-byte UTF-8 characters whole across pty reads. Before, a box-drawing glyph split between two reads became two U+FFFD characters, which made cockpit borders look stacked in recordings (#763).

### Dependencies
- Adds `prompt-toolkit>=3.0.53` (#761).
- `urllib3` 2.8.0 for CVE-2026-97687, CVE-2026-97688 and CVE-2026-97689 (#761).

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
- Cockpit shows every role's selected and observed identity, and links to health evidence (#745)
- `verdict config show` calls the shared config action (#746)
- `verdict trace RUN --node N --panel context` opens a node's context assembly; demo and trace snapshots (#748)
- One health cache and a single prove-at-rest prober (#742)
- Routing explorer shows capacity evidence, pools and cooldown scope, with filters (#755)
- Final recordings and before/after gallery for the redesigned TUI (#747)
- README roadmap states the open backlog is post-alpha (#753)

### Changed
- `verdict prove-at-rest status --json` now reports the health cache; the old per-cycle keys (cycle_id/summary/results) are gone (#742)
- Live cost benchmark with real models (opt-in, `VERDICT_LIVE_SMOKE=1`): the offline catalog router selected the baseline model for every task; no routing saving was measured. Bench script at `scripts/live_savings_bench.py`; proof at `docs/proof/live-savings-2026-09-28/` (#702)
- Provider/gateway bootstrap unified in one offline contract shared by CLI, `verdict serve`, supervisor and library `Gate`; URL userinfo redacted in all diagnostics; `profile: production` refuses default provider sets (#pre-existing in [Unreleased])
- OpenJev influence: context-length failure class; ranking receipts on InfluenceRecord (#670, #667) (single-route `verdict route` ADVISORY path only; `verdict orchestrate` records OpenJev decisions in SHADOW mode)
- `verdict doctor` exits 1 when issues are found (text mode); `--json` mode exits 0 when healthy (#735)
- `verdict harness prime sync-models` writes models.json with owner-only 0600 permissions (#646)
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
- HYDRATE evidence records duplicates and states that compression is not performed (#743)
- Replay honours reduced motion; setup errors show a repair step (#744)
- Qualification timeout judges completion time, not observation time (#754)
- Dependencies: pyjwt 2.13.0 -> 2.15.1 for CVE-2026-102274; the proof workflow installs semgrep as an isolated tool so it cannot downgrade locked packages (#756)
- Catalog-ghost cooldown reads the probe error text (#751)
- Prober: a probe stopped between chat and tool leaves the route unchanged and pending; resume by route id; probe kind follows the capacity class; model identity is verified only on a match, otherwise recorded as mismatch or not_reported (#757)

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
