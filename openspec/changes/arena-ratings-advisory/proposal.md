# Proposal

## Why

Verdict routes prompts to the cheapest proven-capable model (BOD-342 D15).
Capability proof relies on OmniRoute's live metadata store, which records ELO /
intelligence scores but does not capture benchmark evidence from independent
human-preference evaluations (Arena). Arena leaderboard data is a complementary
signal: it reflects real user preferences across chat, web-dev, and agentic
tasks on a large sample of human judges.

This change integrates Chatbot Arena leaderboard data as a reordering-only
(advisory) input to the admission ladder, without introducing any new dependency
beyond the Python standard library.

## What Changes

- **Canonical (org, key) join** (`verdict/arena/`): a new package that parses
  Arena model names into canonical `(org, slug)` keys using an explicit closed
  vocabulary and an explicit crosswalk file, with zero fuzzy or similarity
  matching. The `RestrictedIndex` class raises on any iteration attempt, making
  the no-fuzzy guarantee testable from outside the module.

- **Arena snapshot** (`verdict/arena/snapshot.py`): a JSON-on-disk record of
  resolved store-id-keyed slots (raw score, lower bound, vote count, publish
  date) for three Arena configs (text, webdev, agent). Written atomically;
  loaded lazily on the routing hot path.

- **Verdict Rating (VR)** (`verdict/arena/rank.py`): a 0-100 percentile of a
  model's lower-bound metric against a shared reference set, computed per role
  (planner / implement / reviewer) using per-role metric chains. Never computed
  when |R| < 15. Stale data (leaderboard_publish_date > 45 days) and thin rows
  (below vote/session floors) are excluded from both the route's value and the
  reference set.

- **Fetch** (`verdict/arena/fetch.py`): retrieves Arena data from the Hugging
  Face datasets-server (`datasets-server.huggingface.co`) using stdlib `urllib`
  with a 24-hour staleness gate. Runs only inside `verdict metadata refresh`
  (not on the routing hot path). Host validation is enforced via
  `validate_upstream_url` + `ALLOWED_METADATA_HOSTS` before every `urlopen`
  call. Failed fetches keep the existing snapshot intact.

- **Coverage report** (`verdict/arena/report.py`): a `verdict metadata
  arena-coverage` subcommand that shows match / drop / twin-group counts and
  reference-set sizes per role.

- **Advisory integration** (`verdict/orchestration/eligibility.py`): when a
  node sets `arena_role`, the eligibility ladder augments the `RatedModel` score
  with the computed VR after the standard rating step, but only for
  `tier_known=True` routes. Unknown-tier routes are never promoted by Arena
  evidence.

- **Doctor freshness** (`verdict/doctor_diagnostics.py`): a new `Arena ratings`
  row in `verdict doctor` output: `ok` when fetched_at < 48 h, `warning` when
  fetched_at >= 48 h, `stale` when any leaderboard_publish_date > 45 days.

- **24-hour gate**: `refresh_arena_snapshot` skips configs whose snapshot is
  less than 24 hours old. Called from `verdict metadata refresh`
  (`verdict/actions/extra.py`), which runs inside the existing
  `verdict-metadata-refresh.timer` (already a 6-hour cadence).

## Capabilities

### New Capabilities
- `arena-ratings-advisory`: canonical (org, key) join, VR calculation, snapshot
  persistence, fetch scheduling, coverage report, and advisory eligibility integration.

### Modified Capabilities
- `metadata-refresh`: extended with Arena snapshot fetch (24-hour gate) inside
  the existing `verdict metadata refresh` command.
- `doctor-diagnostics`: extended with Arena freshness row.

## Impact

New package `verdict/arena/` and data file `verdict/data/arena-crosswalk.json`.
Production files modified: `verdict/orchestration/eligibility.py` (lazy VR
augmentation behind `arena_role`), `verdict/orchestration/contracts.py`
(`arena_role` field on `TaskRequirements`), `verdict/orchestration/run.py` and
`verdict/orchestration/review.py` (planner and reviewer nodes set `arena_role`),
`verdict/actions/extra.py` (Arena fetch in metadata refresh),
`verdict/doctor_diagnostics.py` (freshness row), `verdict/metadata/sources.py`
(`ALLOWED_METADATA_HOSTS`, `ARENA_FIELD_MAP`), `verdict/commands/` (new parser
and dispatch entry for `arena-coverage`), `verdict/cli.py`
(`cmd_arena_coverage`).

No new package dependency. All network code uses stdlib `urllib` only.
ADR: [ADR-046](../../../docs/adr/ADR-046-arena-ratings-advisory-source.md).
