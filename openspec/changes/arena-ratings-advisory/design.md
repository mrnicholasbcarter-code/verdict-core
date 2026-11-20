# Design

## Context

Verdict's routing goal is to dispatch each task to the cheapest model proven
capable of that task (BOD-342 D15). The admission ladder admits routes on
hard evidence (OmniRoute health, quota, tier), and the ranker orders admitted
candidates. The ranker currently uses OmniRoute's intelligence score as the
primary sort key within each tier group.

Chatbot Arena (lmarena-ai/leaderboard-dataset on Hugging Face) provides
complementary human-preference data across text, web-dev, and agentic tasks.
Arena data is noisy and sometimes stale; it must augment but never override
admission decisions.

## Goals / Non-Goals

**Goals:**
- Accept human-preference evidence from Arena as a reordering signal for
  admitted routes, without admitting any route, changing any free/paid decision,
  or making any unknown-tier route look healthy.
- Keep the join deterministic and auditable: exact `(org, slug)` match, closed
  vocabularies, explicit crosswalk, zero fuzzy or similarity matching.
- Run Arena fetch outside the routing hot path (inside the scheduled metadata
  refresh, behind a 24-hour staleness gate).
- Keep the new network code inside the existing `ALLOWED_METADATA_HOSTS`
  perimeter and stdlib-only (`urllib`).

**Non-Goals:**
- Admit routes, change free/paid classification, or affect the admitted set
  membership based on Arena evidence.
- Replace OmniRoute live evidence with Arena historical data.
- Support live Arena API calls on the routing hot path.
- Support fuzzy or similarity-based model name matching.

## Decisions

1. **Advisory only, never admission.** Arena VR augments `RatedModel.score`
   after the admission ladder has run and only when `a.tier_known = True`.
   Unknown-tier routes stay in the unknown sort group regardless of their Arena
   VR. The existing invariant "Advisory signals may reorder admitted candidates.
   They never change membership or authorize a route." (ADR-0001 / AGENTS.md)
   is enforced by test.

2. **Exact join, no fuzzy matching.** The store index is wrapped in
   `RestrictedIndex` which raises `RuntimeError` on `__iter__`, `__len__`,
   `.keys()`, `.values()`, and `.items()`. Only `.get()` is exposed. This makes
   the no-fuzzy guarantee testable from outside the module via
   `test_arena_match.py::test_restricted_index_raises_*`.

3. **Canonical `(org, key)` key.** Arena model names are parsed into a
   `Parsed(org, key, effort, qualifiers)` struct. The `org` is mapped via an
   explicit `org_providers` table in the crosswalk. The `key` is a slug of the
   base name with optional mode/date suffixes appended in order. Unknown
   parenthetical tokens are `Drop`s, never silently ignored.

4. **Explicit crosswalk file.** `verdict/data/arena-crosswalk.json` provides:
   - `org_providers`: Arena org name → list of store-side org prefixes.
   - `map`: canonical `(org, key)` → explicit list of `models_dev_ids` (for
     models where Arena uses a different name than the store).
   - `drop`: canonical `(org, key)` → reason string (for Arena rows that are
     permanently unmappable, e.g. aggregate or deprecated entries).
   Each explicit crosswalk target is verified to exist in the store index;
   missing targets are `DROP_CROSSWALK_TARGET_MISSING`, not silent.

5. **VR formula.** `VR_M(x) = 100 * |{r in R : M(r) <= x}| / |R|` where
   `x` = the lower bound (`lo`) of the slot for model M, and R is the reference
   set of store-matched models with usable data for the first config in the
   role's chain where `|R| >= 15`. Lower bound rather than raw score makes the
   VR conservative (penalises high-variance models).

6. **Role metric chains.**
   - `planner`: [arena_text, arena_webdev]
   - `implement`: [arena_agent, arena_webdev, arena_text]
   - `reviewer`: [arena_webdev, arena_text]
   The first config with `|R| >= 15` is used. If none qualifies, VR is `None`.

7. **Floors and staleness.**
   - Vote/session floor: text 5000 votes, webdev 1500 votes, agent 5000
     sessions. Below-floor rows are matched but excluded from VR ranking and
     from the reference set.
   - Staleness: `leaderboard_publish_date > 45 days` → rows excluded from VR.
   - Both are enforced in `_is_usable`; `compute_vr` returns `None` when the
     model's row is excluded.

8. **24-hour fetch gate.** `refresh_arena_snapshot` skips each config whose
   existing snapshot `fetched_at` is less than 24 hours old. This prevents
   redundant network calls on a 6-hour metadata timer without blocking manual
   refreshes (which pass a fresh `now`). Failed fetches keep the prior snapshot.

9. **Atomic snapshot writes.** `save_snapshot` writes to a temp file and calls
   `os.replace` for atomicity. The routing hot path loads the snapshot lazily
   (via `_arena_evidence`) and caches it per `EligibilityLadder` instance.

10. **Doctor freshness row.**
    - `ok`: all `fetched_at` < 48 h ago.
    - `warning`: at least one `fetched_at` >= 48 h ago.
    - `stale`: at least one `leaderboard_publish_date` > 45 days old.
    - `missing`: snapshot file does not exist.
    Status is computed from the on-disk snapshot, not from a network call.

## Risks / Trade-offs

- **[Arena data can be stale or missing]** → Covered by 45-day staleness cutoff
  and 48-hour doctor warning. Arena is advisory; missing data degrades
  gracefully to the standard OmniRoute-score ranking.
- **[Arena org/name drift over time]** → The crosswalk file is the safety
  valve. New models fall through to `DROP_NO_STORE_RECORD` (reported by
  `verdict metadata arena-coverage`) without affecting routing.
- **[Snapshot load on routing hot path]** → Snapshot is loaded lazily once per
  `EligibilityLadder` instance and cached. Load failure falls through to
  `arena_snap = None`, which skips the VR step.
- **[Unknown-tier route accidentally promoted]** → Guarded by the
  `a.tier_known` check before Arena augmentation.

## Migration Plan

Deploy as a new `verdict/arena/` package. Arena integration is dormant until:
1. A node sets `arena_role` in its `TaskRequirements`.
2. The operator has run `verdict metadata refresh` to populate the snapshot.

Existing routing is unaffected by the absence of a snapshot.
