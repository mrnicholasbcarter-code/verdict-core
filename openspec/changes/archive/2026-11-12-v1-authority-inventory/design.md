# Design

## Context

Wave 2 of V1 hardening: S02 (BOD-432) and S04 (BOD-345). Wave 1 is in `openspec/changes/verdict-core-v1-hardening`. The umbrella plan is `docs/contracts/v1-hardening-umbrella-plan.md`.

## Decisions

1. **Server authority**: reserved weakening flags (`allow_legacy_selector`, `require_execution_path_authority`, `execution_path_decision`, `legacy_non_authority_selector`, `bod104_execution_path_authority`, `strategy_authority`, `client_certificate`, `client_cert`, `certificate`, `raw_decision`, `routing_decision`) are rejected with `ExecutionPathError` before selector or send. The relay strips the same set and the typed requirement keys before forwarding upstream.
2. **Price-invariant selection**: `cost_fit` and passport price injection are removed from `verdict/candidate_pool.py` and `verdict/intelligence.py`. `optimize_execution_path` orders qualified offers by trusted `pool_receipt` shortlist position. Missing price stays unknown and gates only under an explicit `hard_spend_budget`. See ADR-035.
3. **No receipt, no selection**: `optimize_execution_path` blocks with `missing_trusted_live_ranking` when `pool_receipt` is `None`, when the shortlist is empty, or when no offer is on the shortlist. Offers off a non-empty shortlist are rejected as `not_on_trusted_shortlist`.
4. **Live probe receipt**: `build_live_probe_receipt` requires `now`, rejects probes in the future or older than `LIVE_PROBE_MAX_AGE_SECONDS` (3600 s), requires `x-omniroute-model` in probe headers, and shortlists only candidates with `eligible is True`. Autodev selection sites and `--execution-path-request` use it or live controller preparation.
5. **Bounded inventory**: per-HTTP timeout 20 s, aggregate deadline 30 s, cumulative decoded budget 16 MiB, 50,000 raw rows, 100 pages. All are parameters with these defaults. Only the adapter-supported `after` id cursor is followed. A violation raises `LiveAdmitError("incomplete_source", ...)` before any cache write or snapshot construction; a last-good copy inside its original `fetched_at` deadline may still mask it.
6. **Unknown versus zero**: an unrecognised catalog shape is unknown (`inventory_complete=False` plus a `shape_unknown` diagnostic). A valid empty `data: []` is an observed zero.

## Risks / Trade-offs

- Passive health (`load_omniroute_health`) keeps its own 20 s timeout outside the 30 s aggregate deadline; it is advisory and never grants eligibility.
- The deployed gateway documents no cursor response field, so the walk degrades to one page today. Paging is proven with mock transports only.
- Autodev ladder rows still set `certification_freshness="fresh"`; freshness comes from the probe age gate.
- The autodev ladder rank mentions price; recorded as an A25 follow-up, not fixed here.
