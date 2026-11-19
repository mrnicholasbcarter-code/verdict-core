# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-11-19

The first complete version. It also carries the 0.5.0 changes below, which were never
published.

This release is not certified: the attested certification rehearsal has not passed yet (see #1168 and #1169).
It is published with the release workflow's bundle requirement switched off for this one
tag and switched back on afterwards; every other release check still runs.
Acceptance gate G7.1 (the quickstart's live route probe) is waived for this release: it
needs an operator-supplied route request with live offers, which the hosted release
runner cannot provide. The other 28 acceptance gates passed.

### Upgrade notes (read first)
- **Behaviour change: `verdict orchestrate` runs workers on `direct-gateway` by default.** Each worker request goes straight to the gateway; no agent harness is needed. Prime Agent is an optional harness: select it with `--executor prime-headless` (the alias `prime` still works). Route visibility follows the executor, so plain eligibility reads the live gateway catalog instead of Prime's `models.json`; `--sync-visibility` still selects Prime, and an `--executor-map` with any Prime node keeps Prime visibility. Without Prime installed, `verdict setup` plans no Prime action, `verdict doctor` shows the Prime rows as `not-installed`, and live acceptance reports `harness-prime` as skipped with its reason, never as passed (#1148).
- **Breaking for library users: `DEFAULT_RUNTIME_STATE_DIR` is removed.** Use `verdict.runtime_contract.default_runtime_state_dir()`, which follows `VERDICT_HOME` on each call (#1143).
- **Behaviour change: the routing decision log moves** from `./verdict-decisions.jsonl` in the working directory to `<VERDICT_HOME>/verdict-decisions.jsonl`. When an old `./verdict-decisions.jsonl` exists next to the new log, readers print one stderr notice that names it and how to read it; it is not read, merged or migrated. Report commands also honour `LLMGATE_LOG_PATH` (#1143).

### Added
- `verdict identify --screen --max-capability-tier N` skips, without probing, routes whose capability tier is unknown or above N (#1147).
- `verdict metrics` computes accepted tasks, dollars, accepted tasks per dollar and capacity shares (free, subscription, metered, unknown) from digest-verified run receipts. Dollars are reported as unknown when any attempt has no cost, and accepted tasks per dollar is then undefined (#1066).
- `verdict prove-at-rest once --reprobe ROUTE` (repeatable) re-runs the agentic probe for an admitted FREE route even when its proof is fresh. Named routes do not count against `--agentic-max-routes`, but do count against `--max-requests` (#1134).
- `verdict ladder --role planner --json`: a read-only, fail-closed role ladder for planner selection (#1059).
- `verdict pr-watch`: polls open pull requests, classifies CI at the exact head SHA, and reruns a failed required job at most once per key. It never merges, pushes or edits source (#1060).
- `POST /v1/route` honours `execution_path_request.hard_spend_budget` and can return its routing trace with `?include_routing_trace=true`. Any value other than a finite number above 0 returns 400 (#1062).
- A worker that writes nothing for `VERDICT_WORKER_IDLE_SECONDS` (default 300) is killed as `stalled` and its node is rerouted within the attempt budget (#1064).
- Certification rehearsals run a bounded FREE-only preflight first and fail closed when no route qualifies (#1067).
- The docs-refresh setup action starts `verdict-docs-refresh.service` once when the docs index is not ready (#1134).
- `verdict identify --screen --min-context-tokens N`, `--require-tools` and `--max-per-pool N` skip, without probing and without using a probe slot, routes below a caller's floor (`skipped_task_floor`; unknown never meets the floor), and cap probes per backend pool, covering pools breadth-first. Without the options the screen output is unchanged (#1151, #1155).
- `verdict doctor` checks a loaded `verdict-prime-sync.service` under `Prime-sync code path` and fails the row when the unit imports `verdict` from a different checkout than the one it is configured for (#903).
- Between 0.5.0 and #1049, among others: an Anthropic Messages relay surface (#997), served-identity verification on every worker and relay attempt (#970), versioned operator policy authority with fail-closed execution boundaries (#1000), qualification producers behind one shared coordinator (#1002), and a definition-of-done gate that checks an orchestrate run against its own result (#1042).

### Changed
- `verdict run-receipt` on a receipt whose event log changed prints `INTEGRITY FAILED` first and labels the stored outcome `recorded (unverified)`. Its `--json` output gains `verified`; on failure `outcome` is `INTEGRITY_FAILED` and the stored outcome moves to `recorded_outcome` (#1141).
- `verdict route --json` prints exactly one JSON document on stdout (#1141).
- `verdict orchestrate --plain` prints the plain narrative on a terminal too, not the live view (#1141).
- First run (#1144): `verdict serve` with a missing setting prints one line that names it and the fix (`verdict setup --apply --allow host.serve`, or exporting `LLMGATE_AUTH_TOKEN` and `VERDICT_RECEIPTS_DB`) and exits non-zero instead of printing a traceback; it still refuses to start. Before setup, `verdict doctor` says "Verdict is installed but not set up yet. Start with: verdict setup" and marks parts that do not exist yet as `not-configured`; it still exits 1 while they remain. `verdict orchestrate` blocked because no free route has a recorded agentic check says so and prints the `verdict identify --screen` command that records one.
- A review whose comment calls were dropped by the review tool is `ERROR` with `findings_lost`, rerun once within the bounded attempts, and never a clean `PASS`. The receipt's review block carries `findings_lost` when it is above zero (#1145).
- Verdict state follows `VERDICT_HOME` everywhere: the offline demo, session ledger, passports and attempt records no longer write to `~/.verdict` when `VERDICT_HOME` is set (#1143).
- The certification rehearsal's preflight screens only routes the planner can admit (`--max-capability-tier 2`), counts only planner-eligible passes, and its failure artifact names the failure class from a closed vocabulary, never raw text (#1147). The rehearsal does not pass yet.
- The certification rehearsal no longer writes the gateway address into its run evidence: it runs `verdict orchestrate` with `VERDICT_REDACT_GATEWAY_ORIGIN=1`, so each `dispatch_permit` controller event, and the receipt's `controller_events`, records the literal `redacted`. The permit check still compares against the real gateway, and the script still refuses to upload run evidence that contains the gateway address or key. Without that variable, `verdict orchestrate` records the origin as before (#1152).
- The certification rehearsal's preflight passes the planner's context and tool floor, probes at most one route per backend pool, screens up to 13 routes (at most 39 model requests) and needs passes in 5 distinct pools (#1155, #1165).
- The chaos rehearsal starts from private copies of the health cache and session-evidence ledger taken right after the preflight, so a 429 in the clean run cannot remove a proven route from chaos, and it fails closed when that proof is missing, invalid or stale (#1157, #1165).
- `verdict doctor` gateway probes wait up to 10 s for `/models` (`VERDICT_DOCTOR_GATEWAY_TIMEOUT_S`) and 3 s for `/api/health` (`VERDICT_DOCTOR_HEALTH_TIMEOUT_S`), retry once on a timeout only (never on an HTTP answer), and name the setting when they give up (#1166).
- CI jobs have timeouts, and the pytest steps dump all thread tracebacks when one test runs over five minutes (#1159).
- The README is rewritten around a two-minute first run, with a replay of a real run's live view as its first image (#1156, #1164). The docs were audited for the public launch (#1162).
- `scripts/live_savings_bench.py` is rewritten as a paired usage bench: the Verdict arm routes through Verdict's own eligibility-gated routing (free and subscription only, no opaque routes, no fallback to the baseline), the baseline model is a required flag, tokens are the headline measure, and same-model pairs or less accepted work block any claim. No result is claimed (#1146).
- An HTTP 200 reply that is an in-band credit refusal (zero output tokens, no tool call, "not enough credits" or "top up" text) is a payment failure, not a success. Probes, the relay and the acceptance serve check all treat it that way (#1130). The ladder health probe scopes it to the route, without a pool or provider cooldown (#1134).
- Routes from prepaid-credit providers without explicit free evidence are classified UNKNOWN capacity, not FREE. `:free` community routes and priced models are unchanged (#1133).
- After an in-band refusal the relay may fail over to the next admitted candidate without an idempotency key. Every other failure still needs the key (#1134).
- A held refresh lease is reported as `lease_held` (exit 75) by `route` and `choose`, apart from `no_eligible_route`. Acceptance waits for the lease for at most 10 minutes and retries once. `verdict-identify-screen.timer` runs at `00/6:20:00` (#1134).
- When the shared coordinator lease is held, selection admits routes with fresh proof and skips probes and confirmations, and still fails closed without fresh proof (#1055).
- Read-only `choose --no-confirm` selects routes with a valid, unexpired exact-route proof even when that proof is older than the 10-minute permit window; invalid or expired proof gets a named reason (#1061).
- Autodev selects its planner from the live eligibility ladder instead of a hardcoded model (#1057).
- `install.sh` works on Debian 12 and Ubuntu 24.04 without pipx: it prefers `uv tool install`, then `pipx`, then a private venv (#1063).
- `verdict doctor` names `verdict harness prime install-service` for a missing `verdict-prime-sync.timer`, and that command renders both units from the installed package. It takes `--dry-run` and `--uninstall`. The `deploy/systemd/verdict-prime-sync.*` templates are removed.
- **Format change:** bootstrap diagnostic lines (`verdict route` notes, `verdict serve` startup lines, `describe_bootstrap_failure`) separate the field from the detail with ` - ` instead of `: `. A parser that splits these lines on `field=<name>:` must split on ` - `. `describe()` also redacts its field, detail and remediation part by part.
- A manual model refresh plan asks once per capacity class (FREE, SUBSCRIPTION, UNKNOWN, METERED), or "free only", instead of once per route. UNKNOWN and METERED default to no and show an estimated spend. Without a terminal, `verdict eligibility --verified --refresh` probes UNKNOWN or METERED routes only with `--include-unknown` or `--allow-metered` (BOD-331).
- **Behaviour change:** the health cache default path follows `VERDICT_HOME`. With `VERDICT_HOME` set, an older `~/.verdict/health-cache.json` is no longer read; route health rebuilds on the next refresh or prove cycle.
- With `--state-file` or `--inject`, `verdict orchestrate` reads `health-cache.json` next to the state file, so isolated and chaos runs start from their own evidence.

### Fixed
- Auto-routers behind a provider prefix (for example `kilocode/kilo-auto/free`, `kilocode/openrouter/free`) are opaque and never candidates (#1053).
- `/v1/route` requests with execution-path candidates use a timezone-aware clock (#1049).
- `verdict harness prime sync-models` keeps the pinned OmniRoute combo `prime-free-chain` in `~/.prime/agent/models.json` across later syncs (#902).
- Planner selection no longer ends with "none eligible" when cold catalog routes would use the whole confirmation budget: fresh cache-positive routes are confirmed first and one slot is kept for a fresh healthy subscription fallback.
- The 1-token health probe treats an HTTP 200 reply with one choice, `finish_reason` `length`, an integer `completion_tokens` and a message with no `content` key as alive, flagged `missing_content` (never `empty_content`), instead of `malformed_response` (#1154).
- On Python before 3.12, `verdict serve` shutdown no longer hangs when the public-models refresh cancel is lost (bpo-42130): a stop flag set before the cancel ends the loop (#1167).
- Invalid planner output is saved (redacted, at most 64 KiB) in the run directory; redaction also covers bare `Bearer` tokens and unlabelled provider key prefixes.

### Removed
- The Ruflo and RuVector integrations (#804). Documentation preflight no longer depends on them.
- Unused scripts and files: six uncalled one-off scripts, the empty `verdict.workflows` package (#1163), `uninstall.sh` (use `verdict uninstall`) and `RELEASE_PACKAGING.md` (folded into `RELEASE_CHECKLIST.md`) (#1161), and 17 duplicate files under `docs/proof/dod` (#1160).

### Known limitations
- The release has no attested certification bundle: the certification rehearsal does not pass yet (#1168, #1169).
- No cost or token saving is claimed. The 2026-09-28 live cost check chose the same model in both arms. One paired usage run on 2026-11-19 did not support a claim: the Verdict arm completed fewer tasks than the baseline, so the bench refused it. Its evidence is not published yet.
- A real HTTP 402 from the ladder health probe still cools the pool and provider; only the HTTP 200 in-band refusal is route-scoped.

## [0.5.0] - 2026-10-09

Not published: no `v0.5.0` tag or PyPI release was made. These changes first ship in 1.0.0.

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
