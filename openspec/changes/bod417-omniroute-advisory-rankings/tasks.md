# Tasks: OmniRoute Advisory Rankings Integration

## 1. Adapter and Fetching

- [ ] 1.1 Create `verdict/omniroute_rankings.py` with `fetch_rankings(gateway: str, category: str) -> Mapping[str, int] | None` using existing `OmniRouteHTTPTransport.bounded_get_json` and `resolve_gateway_binding`. Verify: unit test mocks transport, asserts exact route_id mapping, timeout/exception returns None.
- [ ] 1.2 Add response validation: expect `rankings[]` with `provider`, `topModel.modelId`, `score`, `reliability`, `usage`; sort by score desc; map to exact `provider/model` via existing catalog alias. Verify: test with real API snapshot (`live-authenticated-coding.json`), malformed response returns None.
- [ ] 1.3 Detect non-OmniRoute gateway (URL pattern) and return None with `omission="rankings_gateway_unsupported"`. Verify: test with generic OpenAI gateway URL.

## 2. Category Propagation

- [ ] 2.1 Add `category: str | None` parameter to `select_route_for_task` in `verdict/orchestration/selection.py`. Default `None` for backward compatibility. Verify: existing callers (orchestrate, autodev) still work without changes.
- [ ] 2.2 Add `rankings_category: str | None` parameter to `build_selector` in `verdict/orchestration/eligibility_report.py`. Pass through to `EligibilityLadder`. Verify: `_action_choose_live` computes category from `profile_task` and passes it.
- [ ] 2.3 Store `self._rankings_category` in `EligibilityLadder.__init__`. Verify: test that category flows from choose action to ladder instance.

## 3. Ranking Components Integration

- [ ] 3.1 Modify `EligibilityLadder.__init__` to accept optional `rankings: Mapping[str, int] | None` (already present at line 271 per symbols). Store as `self._rankings`.
- [ ] 3.2 Modify `_rank_components` to compute `advisory_rank`: if `self._rankings` and `self._rankings_category` matches candidate's task family and `route_id` in `self._rankings`, return rank; else `float("inf")`. Verify: unit test with mocked rankings, exact match gets rank 0, non-match gets inf.
- [ ] 3.3 Modify `_rank_key` to include `advisory_rank` at position 5 (after `declared_tier`, before `slack`). Verify: sort order test — two routes same tier/rating, ranked one sorts first.
- [ ] 3.4 Modify `_probe_order` to include `advisory_rank` in sort key after rating. Verify: probe order matches final rank order.

## 4. Receipt and Explain Updates

- [ ] 4.1 Update `_rank_components_for_display` to include `advisory_rank` and ranking metadata (category, source, observed_time, reliability, usage, omission). Verify: test display output matches expected structure.
- [ ] 4.2 Update `explain_selection` in `verdict/orchestration/explain.py` to surface ranking fields in receipt. Verify: `choose --json` output includes `ranking.*` fields.
- [ ] 4.3 Ensure unknown scores are `null` not `0`; no `benchmark_as_of` field. Verify: receipt test asserts `null` for unknown.

## 5. Fallback Behavior

- [ ] 5.1 In `build_selector`, wrap rankings fetch in try/except; on any failure, set `omission` and pass `rankings=None` to ladder. Verify: test each failure mode (timeout, 404, malformed, stale, generic gateway, no match) produces named omission and baseline order.
- [ ] 5.2 Ladder treats `rankings=None` as all `advisory_rank = inf`. Verify: fallback test compares order to no-rankings baseline.

## 6. Tests (RED-First)

- [ ] 6.1 `test_category_propagation_to_ladder` — mock rankings, verify `advisory_rank` present in `_rank_components` only for matching category. Run RED first (should fail before 3.2).
- [ ] 6.2 `test_exact_match_no_transfer` — provide ranking for provider A model X, verify provider A model Y unchanged. Run RED first.
- [ ] 6.3 `test_arena_aa_separation` — verify no arithmetic mixing of Arena (0.4–0.98) and AA (0–100) metrics. Run RED first.
- [ ] 6.4 `test_excluded_top_ranked_regression` — mock top-ranked but quota-exhausted, verify lower-ranked eligible selected. Run RED first.
- [ ] 6.5 `test_fallback_identical_baseline` — simulate each failure mode, compare order to no-rankings baseline. Run RED first.
- [ ] 6.6 `test_named_omission_receipt` — verify `omission` field in receipt for each failure mode. Run RED first.
- [ ] 6.7 `test_receipt_rank_components` — verify JSON structure, `null` for unknown, no `benchmark_as_of`. Run RED first.

## 7. Validation and Proof

- [ ] 7.1 Run `ruff check --fix && ruff format` on changed files. Verify: clean.
- [ ] 7.2 Run `mypy --strict` on changed files. Verify: clean.
- [ ] 7.3 Run focused test suite: `env -u VERDICT_HOME -u VERDICT_OMNIROUTE_API_KEY -u OMNIROUTE_API_KEY PYTHONPATH="$PWD" UV_PROJECT_ENVIRONMENT=/home/nick/dev/verdict-core/.venv uv run --no-sync python -m pytest -q tests/test_omniroute_rankings.py tests/test_eligibility.py tests/test_selection.py tests/test_explain.py`. Verify: all GREEN.
- [ ] 7.4 Run strict OpenSpec validation: `openspec validate bod417-omniroute-advisory-rankings --strict --json`. Verify: valid.
- [ ] 7.5 Parent runs real CLI proof: `verdict choose --task-class implementation --requires tools,code --capacity free --no-confirm --json` from installed merged main. Verify: raw output shows `ranking.*` fields, selected route matches expectations.
- [ ] 7.6 Parent runs OCR on exact integrated head. Verify: OCR passes (fail-closed on invocation error, missing coverage, blocking finding, identity failure, diff mismatch).

## 8. Documentation

- [ ] 8.1 Update any affected CLI help or docs if category flags are user-visible. Verify: `verdict choose --help` accurate.
- [ ] 8.2 Record REUSE.md for AutoCombo/Fusion bounded reuse check (secondary, after working code). Verify: ≤1 page.
