# ADR-046 — Arena Ratings as an Advisory Reordering Source

**Status:** CURRENT
**Date:** 2026-11-20
**Ticket:** BOD-342 (ratings PR B: Arena ratings)

---

## Context

Verdict routes prompts to the cheapest model proven capable of each task.
Capability proof relies on OmniRoute live metadata (health, quota, tier,
intelligence scores). Chatbot Arena (lmarena-ai/leaderboard-dataset, Hugging
Face) provides complementary human-preference rankings across text, web-dev, and
agentic task categories. The data is publicly available, refreshed on a cadence
of roughly weekly, and captures preferences that OmniRoute's internal scores do
not directly reflect.

## Decision

Integrate Chatbot Arena leaderboard data as a **reordering-only advisory
signal** for admitted route candidates. Arena evidence:

1. **Never admits a route.** Admission is controlled by the existing admission
   ladder (health, quota, tier). Arena runs only after admission, in the ranking
   step.
2. **Never changes free/paid classification.** `RatedModel.tier` and
   `tier_known` are set before Arena augmentation and are not modified by it.
3. **Never makes an unknown-tier route look healthy.** VR augmentation is
   gated on `a.tier_known = True`. Unknown-tier routes remain in the unknown
   sort group regardless of Arena rank.
4. **Is advisory, not authoritative.** Exceptions in the Arena path are caught
   and silently skipped. If the snapshot is missing, corrupt, or stale, routing
   continues using OmniRoute scores alone.

This satisfies the invariant from ADR-0001 / AGENTS.md:
> Advisory signals may reorder admitted candidates. They never change membership
> or authorize a route.

## Implementation

### Canonical (org, key) join

Arena model names are parsed into a `Parsed(org, key, effort, qualifiers)`
struct using a closed vocabulary for parenthetical tokens (effort, mode, date).
Unknown tokens are named `Drop`s, never silently ignored. The key is a
normalised slug (`[a-z0-9-]+`).

The store index is wrapped in `RestrictedIndex` which raises `RuntimeError` on
any iteration attempt (`__iter__`, `__len__`, `.keys()`, `.values()`,
`.items()`). Only `.get()` is exposed. This makes the no-fuzzy guarantee
machine-testable.

An explicit crosswalk file (`verdict/data/arena-crosswalk.json`) handles:
- `org_providers`: Arena org name → list of store-side org prefixes.
- `map`: explicit `(org, key)` → one or more `models_dev_id` overrides.
- `drop`: explicit `(org, key)` → permanent drop with named reason.

Each explicit map target is verified against the live store index; a missing
target is `DROP_CROSSWALK_TARGET_MISSING`, not silent.

### Verdict Rating (VR) formula

```
VR_M(x) = 100 * |{r in R : M(r) <= x}| / |R|
```

where:
- `x` is the lower bound (`lo`) of the Arena metric for model M (conservative:
  penalises high-variance models).
- `R` is the reference set of store-matched models with usable data for the
  first config in the role's chain where `|R| >= 15`.

### Role metric chains

| Role       | Chain                                  |
|------------|----------------------------------------|
| planner    | arena_text, arena_webdev               |
| implement  | arena_agent, arena_webdev, arena_text  |
| reviewer   | arena_webdev, arena_text               |

### Floors and staleness

| Config      | Floor         | Staleness cutoff          |
|-------------|---------------|---------------------------|
| arena_text  | 5000 votes    | leaderboard_publish_date > 45 days |
| arena_webdev| 1500 votes    | same                      |
| arena_agent | 5000 sessions | same                      |

Rows below the floor are matched but excluded from VR ranking and from the
reference set. Stale rows are excluded entirely.

### Fetch scheduling

Arena fetch runs inside `verdict metadata refresh` (the existing
`verdict-metadata-refresh.timer`, cadence 6 h). A 24-hour staleness gate
prevents redundant fetches: each Arena config is skipped when its `fetched_at`
is less than 24 hours old. Network code uses stdlib `urllib` only (no new
dependency). The host `datasets-server.huggingface.co` is added to
`ALLOWED_METADATA_HOSTS` and validated via `validate_upstream_url` before every
`urlopen` call.

### Doctor integration

`verdict doctor` reports an `Arena ratings` row:
- `ok`: all `fetched_at` < 48 h ago.
- `warning`: at least one `fetched_at` >= 48 h ago (refresh overdue).
- `stale`: at least one `leaderboard_publish_date` > 45 days old.
- `missing`: snapshot file does not exist.

## Consequences

- Positive: Arena human-preference signal supplements OmniRoute intelligence
  scores, particularly for routing tasks with strong human-preference signals
  (chat quality, web-dev usability, agentic success).
- Positive: The exact-join design prevents silent mis-mappings. Coverage gaps
  are reported by `verdict metadata arena-coverage`.
- Neutral: Routes without an Arena record or with thin/stale data rank by
  OmniRoute score only (no regression from today's behaviour).
- Neutral: The 24-hour gate means Arena data is at most ~30 hours old during
  normal operation (6-hour metadata timer + 24-hour arena gate).
- Risk: Arena leaderboard model names drift over time. The crosswalk file is the
  operator-managed safety valve; new unrecognised names fall through to
  `DROP_NO_STORE_RECORD` without affecting routing.

## Alternatives Considered

1. **Fuzzy name matching**: rejected. Fuzzy matching can silently map Arena rows
   to incorrect store entries. The `RestrictedIndex` + closed-vocabulary design
   makes the no-fuzzy guarantee testable.
2. **Direct admission weight from Arena**: rejected. Arena evidence is
   historical and task-averaged; it must not override OmniRoute's live capacity
   and health signals in admission.
3. **Separate background process for Arena fetch**: rejected. The existing
   `verdict-metadata-refresh.timer` already fires at 6-hour cadence. A 24-hour
   gate inside that timer is simpler and avoids a second timer unit.

## References

- ADR-0001: Verdict control-plane invariants (admission ladder, advisory signals)
- ADR-036: Goal-to-receipt orchestration (planner, worker, reviewer roles)
- ADR-042: Dynamic role model selection (role-based `TaskRequirements`)
- OpenSpec change: `openspec/changes/arena-ratings-advisory/`
- Crosswalk: `verdict/data/arena-crosswalk.json`
- Proof test: `tests/test_arena_match.py::test_restricted_index_raises_*`
