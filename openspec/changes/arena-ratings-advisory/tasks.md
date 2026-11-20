# Tasks

## 1. Canonical (org, key) join and crosswalk

- [x] 1.1 `verdict/arena/canonical.py`: `parse_name(model_name, org, org_providers)` → `Parsed | Drop`. Closed vocabularies for effort, mode, date tokens. `slug()` normalises to `[a-z0-9-]+`. Unknown tokens are named `Drop`s, never silent. `DROP_*` constants exported.
- [x] 1.2 `verdict/arena/match.py`: `RestrictedIndex` wraps store index dict and raises `RuntimeError` on `__iter__`, `__len__`, `.keys()`, `.values()`, `.items()`. `build_store_index(store_ids)` returns a `RestrictedIndex`. `match_rows()` applies canonical join or explicit crosswalk; validates crosswalk targets against store index; collision detection.
- [x] 1.3 `verdict/data/arena-crosswalk.json`: explicit `org_providers` table, `map` entries for divergent names, `drop` entries for permanently unmappable rows. Lint rules: `org_providers` must cover every `map`/`drop` entry; each `models_dev_id` must contain `/`.

## 2. Arena snapshot

- [x] 2.1 `verdict/arena/snapshot.py`: `ArenaSlot` (raw, lo, hi, n, published, via), `ArenaSnapshot` (schema_version, fetched_at, leaderboard_publish_date, reference_sets, records), `CONFIG_TEXT/WEBDEV/AGENT`, `FLOOR_N`, `STALENESS_DAYS=45`. `load_snapshot()` returns `None` on missing/corrupt. `save_snapshot()` atomic write.

## 3. Verdict Rating (VR)

- [x] 3.1 `verdict/arena/rank.py`: `compute_vr(store_id, snapshot, role_key, now)` → `ArenaVR | None`. `_vr(x, ref_values)` computes 0-100 percentile. `_is_usable()` applies vote/session floors and 45-day staleness. `vr_for_route(route_id, snapshot, role_key, identity_map, now)` resolves route_id → store_id via `identity_map.models_dev_id_for()`. `build_reference_sets()` mutates the snapshot before save.
- [x] 3.2 Role chains: `planner` → [arena_text, arena_webdev]; `implement` → [arena_agent, arena_webdev, arena_text]; `reviewer` → [arena_webdev, arena_text]. First chain entry with |R| >= 15 wins. VR is `None` when no chain entry qualifies.

## 4. Fetch

- [x] 4.1 `verdict/arena/fetch.py`: `refresh_arena_snapshot(store_ids, ...)` → status dict. 24-hour freshness gate per config. Failed fetch keeps prior snapshot. `_validate_url()` calls `validate_upstream_url` + checks `ALLOWED_METADATA_HOSTS`. `# nosec B310` on `urlopen` calls. No new dependency (stdlib `urllib` only). `datasets-server.huggingface.co` added to `ALLOWED_METADATA_HOSTS`.
- [x] 4.2 `verdict/actions/extra.py`: `cmd_refresh_metadata` calls `refresh_arena_snapshot` after a successful metadata refresh, only when not offline.

## 5. Advisory integration

- [x] 5.1 `verdict/orchestration/contracts.py`: `arena_role: str | None = None` on `TaskRequirements`. Values: `"planner"`, `"implement"`, `"reviewer"`, or `None` to skip.
- [x] 5.2 `verdict/orchestration/eligibility.py`: `_DEFAULT_ARENA` sentinel; lazy `_arena_evidence()` loader mirroring `_DEFAULT_METADATA`. After `rate_model()`, when `arena_role is not None and a.tier_known`, augment `a.rating` with Arena VR via `dataclasses.replace()`. Exceptions silently skipped (advisory).
- [x] 5.3 `verdict/orchestration/run.py`: planner node sets `arena_role="planner"`.
- [x] 5.4 `verdict/orchestration/review.py`: reviewer node sets `arena_role="reviewer"`.

## 6. Coverage report

- [x] 6.1 `verdict/arena/report.py`: `coverage_report()` returns matched/unmatched/twin-group counts, reference-set sizes per role, snapshot meta. `print_coverage_report()` formats for terminal or JSON.
- [x] 6.2 `verdict/commands/parsers_autodev.py`: `metadata arena-coverage` subparser with `--json` and `--store`/`--snapshot` overrides.
- [x] 6.3 `verdict/commands/dispatch.py` and `verdict/cli.py`: dispatch and implementation of `cmd_arena_coverage`.

## 7. Doctor freshness

- [x] 7.1 `verdict/doctor_diagnostics.py`: `arena_snapshot_row(*, now)` returns `{label, status, detail}` with statuses `missing/ok/warning/stale`. Inserted after `health_cache_row` at both call sites.

## 8. Tests

- [x] 8.1 `tests/test_arena_canonical.py`: slug normalisation table, `parse_name` parametrized table, Drop near-misses, one-char perturbation, AST no-fuzzy scan (confirms no fuzzy-match imports).
- [x] 8.2 `tests/test_arena_match.py`: `RestrictedIndex` raises on 5 ops, no-fuzzy one-char miss, exact match, effort slot, variant collision, crosswalk drop.
- [x] 8.3 `tests/test_arena_rank.py`: VR range [0, 100], floor exclusion, staleness exclusion, permutation (max-lo > min-lo VR), None for missing model, reference set >= 15, role chain fallback.
- [x] 8.4 `tests/test_arena_fetch.py`: `_is_fresh` fresh/stale/missing, refresh skips when fresh (mocked), refresh keeps old snapshot on failure.
- [x] 8.5 `tests/test_arena_report.py`: coverage matched >= 1, by_config has text/webdev/agent, twin group detection, no twins for distinct keys, JSON output valid.
- [x] 8.6 `tests/test_arena_doctor.py`: missing/ok/warning/stale states.

## 9. Architecture decision record

- [x] 9.1 `docs/adr/ADR-046-arena-ratings-advisory-source.md`: records the decision to use Arena as an advisory source, the exact-join invariant, the VR formula, role chains, floors, staleness cutoff, and why Arena evidence never affects admission.
- [x] 9.2 `docs/adr/README.md`: add ADR-046 to the index table.

## Scope and Delivery Notice

- **Active Scope**: Pre-merge code implementation and test verification only.
- **DoD**: Ticked tasks (`[x]`) represent pre-merge implementation and focused test verification. This change is not archived until the operator verifies live commands (`verdict metadata arena-coverage`, `verdict doctor`) on merged main from the real host.
- - [ ] (post-merge) `verdict metadata refresh` populates the Arena snapshot on the operator's host.
- - [ ] (post-merge) `verdict doctor` shows `Arena ratings: ok` on merged main.
- - [ ] (post-merge) `verdict metadata arena-coverage` runs without error on merged main.
