# Design

## Context

See proposal.md - Why. Verdict's shared production selector (`build_selector` → `EligibilityLadder` → `select_route_for_task` → `explain_selection`) already ranks gate-admitted routes using capacity tier, rating, slack, quota rank, provider preference, load and fit. `verdict/actions/extra.py::_action_choose_live` is the user-facing chooser and computes a task family but currently passes only `TaskRequirements` (coding bool) into selection, losing coding/debugging/review category. OmniRoute exposes `GET /api/free-provider-rankings?category=...` returning provider-level best-scored model evidence. Hard admission (authentication, entitlement, capability, quota, capacity, availability, authority, risk) remains the sole authority; rankings are advisory only among admitted routes.

## Goals / Non-Goals

**Goals:** Consume optional OmniRoute free-provider rankings as task-aware advisory in the shared eligibility ladder for coding, debugging, and review; preserve existing admission and receipt/provenance contracts; safe generic-gateway fallback; strict OpenSpec validation; parent-owned exact-head OCR.

**Non-Goals:** Duplicate router, selector, cache, database, telemetry subsystem, or probe loop; change hard admission weights or default controller; make OmniRoute a hard dependency; score provider-best evidence onto unrelated models; mix Arena and AA metrics numerically; invent benchmark `as_of` timestamps; declare unknown scores as zero.

## Decisions

- **Rankings as advisory overlay, not ranker:** `EligibilityLadder` keeps `_rank_components`/`_rank_key` as the single ordering source. Rankings add an `advisory_rank` component used only as a tie-break among already-admitted routes, consistent with existing `_rank_key` order. `_probe_order` scheduling remains unchanged unless a rankings-aware ordering is proven consistent with confirmation scheduling.
- **Exact matching only:** Score exact provider/model or existing trusted canonical aliases only. No fuzzy version-prefix matching. No transferring a provider's best-model score to that provider's other models.
- **Metric separation:** Arena scores (0.4-0.98) and AA coding scores (0-100) remain in separate source components; never arithmetically mixed.
- **Unknown is unknown:** Missing/stale/unmatched ranking data yields `advisory_rank` omission with named reason; baseline ordering is unchanged; scores are never defaulted to zero.
- **Provider reliability is advisory:** `reliability.state/connections/usage` is provider-aggregate context, not exact-route health or entitlement; no invented numeric weights or TTL.
- **Bounded fetch:** Reuse existing OmniRoute HTTP transport and credential separation; bounded GET; 404/timeout/malformed → named omission + baseline; no unsafe urllib client.
- **Category plumbing:** `verdict/actions/extra.py::_action_choose_live` computes coding/debugging/review category from `task_class`/`task` text and propagates into `TaskRequirements`/`selection` with backwards-compatible default (coding/general for other callers); no `WorkNode` overhaul.
- **Tests first:** Tracked RED tests for actual `rankings[]` API shape, exact matching, mismatch, alias, metric separation, excluded-top regression, and fallback before production edits.

## Risks / Trade-offs

[Rankings fetch adds external dependency] → bounded optional fetch with named omission and baseline fallback; no hard dependency.
[Provider-level score misread as model-level] → exact provider/model matching; documented evidence boundary.
[Arena vs AA metric mixing] → separate source components; tests enforce separation.
[Stale snapshot mistaken for fresh benchmark] → report observed fetch timestamp as unknown; never fabricate `benchmark as_of`.
[Worker dispatch boundary failure] → two consecutive healthy workers (Codestral, Nemotron) exited `done` without required reply; spec authored by controller as design artifact; implementation will be redispatched under exact route fence.

## Migration Plan

No data migration. Additive optional component; existing behavior unchanged when rankings are unavailable. Rollback: remove rankings component and revert `_rank_components`/`_rank_key` to baseline; no schema changes.



## Security / trust implications

Rankings are advisory and never become permission. Unknown capability never becomes permission (ADR-020). Credentials, raw headers, endpoints, prompts, responses, and private gateway state remain outside the contract and are rejected at the boundary (ADR-020). No secret is stored, logged, or copied in body output; auth uses the existing resolver only.

## Routing / context / memory implications

OmniRoute provides inventory, transport, health, quota, and evidence; it does not own routing strategy (ADR-0001). Gateways do not own ranking strategy. No new memory store, context pipeline, or database is introduced. The change is additive to the existing eligibility ladder.

## Migration / rollback

No data migration. Additive optional component; existing behavior unchanged when rankings are unavailable. Rollback: remove the rankings component from `_rank_components`/`_rank_key` and revert to baseline ordering; no schema or config changes.

## ADR impact

ADR-0001 (control-plane invariants): hard eligibility precedes advisory ranking; gateway evidence does not own strategy. No ADR file changes. ADR-020 (gateway adapter contracts): provider-neutral boundary; unknown is not permission. No ADR file changes. Accepted ADRs govern this slice; this artifact records their disposition only.


## Open Questions

None.
