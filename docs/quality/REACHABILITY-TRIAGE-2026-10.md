# Reachability Triage — 2026-10

BOD-316: "done means wired". This is a TRIAGE + CHECK document, not a wiring
pass. It records a decision for each named example in BOD-316 plus the
automated tool's top findings. No code is wired or deleted in this story;
see the "Decision key" below for what each decision means.

Updated against `scripts/check_reachability.py` (BOD-316) after the
round-1 resolver and production-root fixes on `chore/bod-316-reachability`.
The original triage used vulture and Code Review Graph as second opinions.
This revision uses qualified AST references and reviewer probes. Vulture
is not installed in the local interpreter; CI pins 2.16. See the checker
module docstring for the remaining static-analysis limits.

## Decision key

- **WIRE: BOD-XXX** — a production caller should be added; an existing
  story owns that work (named). This story does not add the wiring.
- **DELETE: defer to BOD-259** — the code looks like dead weight; deletion
  is deferred to the dead-code cleanup story (BOD-259) so one story owns
  all deletions instead of scattering them across unrelated branches.
- **ALLOWLIST: reason** — intentionally unreachable in production right
  now (a stub pending a future activation story, test-only scaffolding
  that is not itself a "feature", or a deliberate escape hatch) and will
  stay in `reachability-baseline.json` until that reason changes.

## Named examples (from the BOD-316 story text)

| Item | Finding | Decision |
|---|---|---|
| `MemoryMirrorWorker.run_once` (BOD-146 outbox/mirror) | `verdict/memory_mirror.py` — referenced only from `tests/test_memory_outbox.py` and `tests/test_cross_harness_recall.py`; `verdict/actions/extra.py`'s `daemon.run_once()` calls a *different* `run_once` (on `prove_at_rest`'s prober, not `MemoryMirrorWorker`) | **WIRE: BOD-146** — the mirror worker needs a production call site (a CLI action or a scheduled job), same as `prove_at_rest`'s daemon has one. |
| `SharedMemoryCapabilityProvider` (BOD-146) | `verdict/context_sources.py` — absent from `build_native_providers()`; only constructed in `tests/test_shared_memory_context.py` | **WIRE: BOD-146** — same story as the mirror worker; the provider and the worker that feeds it should land together. |
| `verdict/compaction.py` (BOD-38) | The module has 15 public top-level definitions. There is no external production import of `verdict.compaction` in `verdict/` or `scripts/`; 5 entry helpers are `test_only_caller`, while the other 10 have same-module references. Internal references do not establish a production entry path. | **WIRE: BOD-38** — confirmed: the module has zero production callers. BOD-38 (or a successor) owns wiring this into the harness/session continuity path it was designed for. |
| `subagent_selection.select_worker_model` / `execute_with_worker_failover` | `verdict/subagent_selection.py` — both referenced only from `tests/test_worker_admission.py`, `tests/test_worker_provider_policy.py`, `tests/test_subagent_selection.py`, `tests/test_worker_runtime.py` | **WIRE: BOD-157** — this is worker-model selection/failover for the multi-story governor; `verdict/orchestration/supervisor_admission.py` (the BOD-157 governor pipeline) is the natural caller and does not currently import this module. |
| `outcome_records` (write-only) | `build_outcome_records`/`write_outcome_records` in `verdict/outcome_records.py` ARE called in production: `verdict/orchestration/receipt.py::write_run_receipt` calls `write_outcome_records`. Not flagged by the tool. | **ALLOWLIST: not a finding** — the story's premise ("write-only") describes the read side, not reachability: the module is *called*, but nothing in `verdict/` reads `outcome-records.jsonl` back. That is a data-flow gap, not a reachability gap, so it is out of `check_reachability.py`'s scope; recorded here for the record, no baseline entry needed. |
| `adaptive_ranker` (observe-only) | `AdaptiveRanker`/`build_adaptive_ranker` ARE called in production: `verdict/decision_kernel.py::_baseline_ranker` calls `build_adaptive_ranker()`, and `decide()` consults it when `advisory` is passed. Not flagged as unreachable. `AdaptiveRanker.get_canary_status` and `rollback` have test-only callers at `tests/test_adaptive_ranker.py:109` and `:207`. Only `record_outcome` is uncalled. | **ALLOWLIST: ranker itself is wired (not a finding)**; **DELETE candidate: BOD-259** for the uncalled `record_outcome`; review the test-only canary/status and rollback methods before removal. No production feedback path was found. These are methods, outside the top-level scan and baseline, not evidence that their tests are absent. |
| `run.py` static `max_parallel=3` vs. the BOD-157 governor | `verdict/orchestration/run.py::run_golden_path` defaults `policy: RuntimePolicy = RuntimePolicy()`; `RuntimePolicy.max_parallel` is a static dataclass default of `3` (`verdict/orchestration/runtime.py`), and `run_golden_path` forwards `policy.max_parallel` straight into `plan_with_failover(max_parallel=policy.max_parallel, ...)` — never through `GlobalConcurrencyGovernor.admit()` (`verdict/orchestration/concurrency_governor.py`), which is only reachable via `verdict/orchestration/supervisor_admission.py`'s separate admission pipeline | **WIRE: BOD-157** — same governor-integration gap as `select_worker_model` above: the governor pipeline exists and is tested, but `run_golden_path`/`plan_with_failover` never calls it to size `max_parallel`. |
| `capability_registry` Serena/Context7/codebase-memory adapter stubs | `make_serena_lsp_stub`, `make_context7_stub`, `make_codebase_memory_stub` (plus `make_agentshield_stub`, `make_gitleaks_stub`, `make_semgrep_security_stub`) all default `health="unavailable"`; `build_default_registry()` forwards that same default via `enrichment_health: ProviderHealth = "unavailable"` | **ALLOWLIST: intentional default-degraded posture, pending each adapter's own activation story.** `NativeCapabilityResolver`/`build_native_providers()` already give every capability a native fallback (per `CONTRIBUTING.md`'s "Explicit degradation" principle), so an adapter defaulting off is not silently losing coverage — it is documented ("Optional ... enrichment; default unavailable until health probe passes"). Each adapter needs its own health-probe + wiring story before its default can flip; no single BOD number owns "turn on Serena" today, so this stays allowlisted rather than assigned to a placeholder story number. |

## Automated tool's top findings (by file, descending count)

`scripts/check_reachability.py --json`: 2099 public symbols checked in
`verdict/`, 236 findings (52 `uncalled`, 178 `test_only_caller`, 6
`stub_default`), 0 NEW. All 236 are in `reachability-baseline.json` with
specific grouped reasons or an honest one-line `UNTRIAGED` observation.
192 entries remain `UNTRIAGED`; this baseline is not a claim of full triage.
The table below retains the original triage groups, not a new count ranking.

Files with the most findings:

| File | Findings | Decision |
|---|---|---|
| `verdict/api.py` | 0 | FastAPI attribute-call decorators now count as production roots. No route-handler exceptions remain in the baseline. |
| `verdict/decision_kernel_demo.py` | 7 | **ALLOWLIST: demo/fixture module.** The module's own docstring says it "builds in-memory inputs" for tests and docs; `tests/test_decision_kernel.py`, `tests/test_enforcement.py`, and `tests/test_memory_gate.py` import it explicitly as a fixture provider, which is its designed role, not a gap. |
| `verdict/capability_registry.py` | 6 | See the named-examples row above (Serena/Context7/codebase-memory/agentshield/gitleaks/semgrep stubs). |
| `verdict/compaction.py` | 5 | See the named BOD-38 row above — these 5 are a subset of the module's symbols; the module-level finding (no import anywhere) is the real signal. |
| `verdict/autodev_routing.py` | 4 | **DELETE: defer to BOD-259.** `OpenAICompatibleEvidenceAdapter`, `attest_response`, `discover_concrete_routes`, `normalize_retry_safety` are referenced only from `tests/test_autodev_routing.py`-family tests; no production module imports `verdict.autodev_routing` outside of `verdict/autodev_run.py` (which does not call these four). Needs a maintainer decision on whether autodev routing is still a live feature; out of scope to decide here. |
| `verdict/memory_migration.py` | 4 | **DELETE: defer to BOD-259.** One-shot migration helpers referenced only from their own tests; migrations are typically run once and then become historical — worth confirming the migration already ran before deleting. |
| `verdict/release/normalize.py` | 4 | **DELETE: defer to BOD-259.** `findings_from_bandit_json`, `findings_from_npm_audit_json`, `findings_from_pip_audit_json`, `sbom_artifact_from_cyclonedx_json` are referenced only from `tests/security/test_normalize.py`. Checked: `scripts/certify_release.py` (the actual release-gate bandit/pip-audit runner) does not import `verdict.release.normalize` — it parses bandit/pip-audit output itself with `xml.etree.ElementTree`/`json`. This normalize module looks like parallel, unused tooling built for `verdict/release/evidence.py`'s `Finding`/`SBOMArtifact` types but never wired to a producer; confirm with the release-evidence owner before deleting. |
| `verdict/subscription_headroom.py` | 4 | **DELETE: defer to BOD-259.** Test-only helpers for subscription headroom math; no production caller found. |
| `verdict/anthropic_messages_adapter.py` | 3 | **ALLOWLIST: public adapter API surface,** called by downstream integrations outside this repo's `verdict/` tree (the Anthropic Messages-compatible endpoint contract); kept test-covered pending a BOD number for the production wiring. |
| `verdict/autodev_run.py` | 3 | **DELETE: defer to BOD-259**, same autodev-routing question as above. |

## What this story does NOT do

Per BOD-316 scope ("TRIAGE + CHECK, not a wiring effort"): no code in
`verdict/` is wired, no code is deleted, and no stub's default health is
flipped. The decisions above assign an owner (an existing BOD story) or an
explicit allowlist reason; follow-up stories do the actual wiring/deletion.
