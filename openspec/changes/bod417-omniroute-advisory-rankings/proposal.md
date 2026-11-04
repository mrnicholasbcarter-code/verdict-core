# Proposal: BOD-417 OmniRoute advisory rankings into Verdict eligibility ladder

## Why

Verdict's `verdict choose` and shared selection path rank admitted routes using capacity, rating, slack, quota, load and fit, but never consult OmniRoute's free-provider rankings as a task-aware advisory. BOD-417 requires that coding, debugging and review categories flow from `verdict choose` into the shared production selector and consume optional OmniRoute free-provider rankings only after canonical admission, with named omission and baseline ordering when rankings are missing, stale, malformed, 404, timeout, or from an unsupported/generic gateway. No duplicate router, cache, database, telemetry subsystem, or probe loop.

## What Changes

- Add optional OmniRoute free-provider rankings advisory into `verdict/orchestration/eligibility.py::EligibilityLadder` ranking components and `verdict/orchestration/eligibility_report.py::build_selector`, consumed only after hard admission.
- Propagate coding/debugging/review task category from `verdict/actions/extra.py::_action_choose_live` into the selector/contracts with backwards-compatible default.
- Keep one consistent ordering source across `_rank_components`, `_rank_key`, and `_probe_order`; never numerically mix Arena and AA metrics.
- Preserve authentication, entitlement, capability, quota, capacity, availability, authority and risk gates as authoritative; an excluded top-ranked route cannot win.
- Parse the actual top-level `rankings[]` response; score only exact provider/model or existing trusted canonical aliases; no fuzzy prefix transfer or provider-wide score transfer.
- Provider reliability is advisory context, never exact-route health or entitlement.
- Missing/stale/malformed/404/timeout/generic-gateway paths produce a named omission and identical baseline order; no hard OmniRoute dependency.
- Add tracked red-first tests for exact matching, mismatch, alias, metric separation, excluded-top regression, and fallback behavior.
- Add `openspec/changes/bod417-omniroute-advisory-rankings` spec delta for `095-evidence-based-model-chooser`.

## Capabilities

### New Capabilities

- `omniroute-advisory-rankings`: optional OmniRoute free-provider rankings consumed as a task-aware advisory tie-break inside the shared eligibility ladder, after hard admission, with exact-match scoring, named fallback, and receipt provenance. Extends the existing `095-evidence-based-model-chooser` behavior (which lives under `specs/`, not the OpenSpec capability store) without modifying it.

## Impact

- `verdict/orchestration/eligibility.py` — `_rank_components`, `_rank_key`, `_probe_order`, advisory ranking consumption.
- `verdict/orchestration/eligibility_report.py` — `build_selector` rankings wiring.
- `verdict/orchestration/selection.py` — category propagation passthrough if needed.
- `verdict/actions/extra.py` — `_action_choose_live` task-category plumbing.
- New `verdict/omniroute_rankings.py` — optional OmniRoute rankings fetch/parse (bounded HTTP transport, existing credential resolver, no unsafe urllib).
- `tests/test_omniroute_rankings.py` — tracked red/green unit tests.
- Neighbor suites: `tests/test_orchestration_eligibility.py`, `tests/test_orchestration_selection.py`, `tests/test_actions_extra.py`.
- `openspec/changes/bod417-omniroute-advisory-rankings/specs/095-evidence-based-model-chooser/spec.md` — spec delta.

## Evidence boundary

KNOWN: live `/api/free-provider-rankings?category=coding|debugging|review|documentation|default&configuredOnly=1&availableOnly=1&withUsage=1&usageRange=24h` returns top-level `rankings[]` with `providerId`, `category`, `topModel.modelId/modelName/score/eloRaw/confidence/category`, `averageScore/modelCount`, `reliability.state/connections/usage`. KNOWN: provider best score is not per-model whole catalog and not all models are free. KNOWN: Arena scores 0.4-0.98 are not comparable to AA coding 0-100. INFERRED: score only exact provider/model or existing canonical aliases; unknown stays null/unknown. NOT AVAILABLE: benchmark `as_of` timestamp from API; provider-level free status does not prove exact-route entitlement or health.

## Acceptance criteria

AC1. Coding, debugging, and review task categories flow from `verdict choose` into the shared production selector; optional OmniRoute rankings act only after canonical admission.
AC2. Only an exact provider/model match or an existing trusted canonical alias receives a ranking score; provider-best evidence never transfers to unrelated routes; Arena and AA metrics remain separate.
AC3. Authentication, entitlement, capability, quota, capacity, availability, authority, and risk exclusions remain authoritative; a higher-ranked excluded route cannot be selected.
AC4. Missing, malformed, stale, 404, timeout, unsupported/generic-gateway, and unmatched ranking data produce a named omission and identical baseline ordering.
AC5. Ranking provenance, category, observed timestamp, reliability/usage context, and omission reason appear in existing rank components/explanations; unknown scores are not zero-filled; no fabricated benchmark date.
AC6. Accepted ADR-0001 and ADR-020 govern the design; an OpenSpec delta maps these scenarios and validates strictly; parent-owned exact-head OCR fails closed on invocation error, missing coverage, invocation failure, or blocking finding.
