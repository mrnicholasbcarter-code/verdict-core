# Tasks

## 1. Provider Identity Foundation

- [ ] 1.1 Add `CODIV_PROVIDER_PREFIX` configuration (default `"codiv"`)
- [ ] 1.2 Implement `is_codiv_route(provider: str, prefix: str = "codiv") -> bool` exact match function
- [ ] 1.3 Test: inventory with `nvidia/diffusiongemma-26b-a4b-it` → 0 Codiv candidates
- [ ] 1.4 Test: inventory with `codiv/diffusiongemma-26b-a4b-it` → 1 Codiv candidate
- [ ] 1.5 Test: inventory with `bm/diffusiongemma-26b-a4b-it` → 0 Codiv candidates

## 2. Failure Classification Extensions

- [ ] 2.1 Check if `NormalizedFailureClass.QUOTA_EXHAUSTED` exists, add if missing
- [ ] 2.2 Add `PROVIDER_OVERLOAD` to `NormalizedFailureClass` (or reuse `UPSTREAM`)
- [ ] 2.3 Implement 429 response body parsing for quota vs rate-limit indicators
- [ ] 2.4 Map 529 to provider capacity/overload class
- [ ] 2.5 Test: 429 with quota-exhausted body → `QUOTA_EXHAUSTED`, no retry
- [ ] 2.6 Test: 529 → provider capacity class (not quality degradation)

## 3. Retry-After Parsing

- [ ] 3.1 Implement `parse_retry_after(header: str | None, min_sec=1, max_sec=300) -> int`
- [ ] 3.2 Handle seconds (integer) format
- [ ] 3.3 Handle HTTP-date format (RFC 7231)
- [ ] 3.4 Clamp negative values to min (1s)
- [ ] 3.5 Clamp huge values to max (300s)
- [ ] 3.6 Return default bounded value on garbage/missing input
- [ ] 3.7 Test: `Retry-After: 60` → 60s cooldown
- [ ] 3.8 Test: `Retry-After: -1` → clamped to 1s
- [ ] 3.9 Test: `Retry-After: 999999` → clamped to 300s
- [ ] 3.10 Test: `Retry-After: garbage` → default bounded cooldown
- [ ] 3.11 Test: missing Retry-After → default bounded cooldown

## 4. Receipt Provider Identity

- [ ] 4.1 Add `intended_provider: str | None = None` to receipt dataclass/dict
- [ ] 4.2 Add `intended_model: str | None = None` to receipt dataclass/dict
- [ ] 4.3 Add `executed_provider: str | None = None` to receipt dataclass/dict
- [ ] 4.4 Add `executed_model: str | None = None` to receipt dataclass/dict
- [ ] 4.5 Add `provider_mismatch: bool = False` to receipt dataclass/dict
- [ ] 4.6 Populate intended fields from routing selection
- [ ] 4.7 Populate executed fields from actual execution
- [ ] 4.8 Set `provider_mismatch=True` when intended ≠ executed
- [ ] 4.9 Test: matching provider/model → `provider_mismatch=False`
- [ ] 4.10 Test: fallback scenario → `provider_mismatch=True`
- [ ] 4.11 Verify backward compatibility (legacy receipts still valid)

## 5. Eligibility and Absence Safety

- [ ] 5.1 Verify Codiv routes pass through standard DISCOVERED → ENTITLED → HEALTHY → AVAILABLE → TASK_ELIGIBLE gates
- [ ] 5.2 Test: missing `CODIV_API_KEY` → Codiv routes ineligible with reason
- [ ] 5.3 Test: missing `CODIV_API_KEY` → other provider routes unaffected
- [ ] 5.4 Test: protected work + Codiv + paid route → not forced to Codiv without positive availability

## 6. Documentation

- [ ] 6.1 Create `docs/providers/codiv.md` with hermetic vs operational split
- [ ] 6.2 Document provider prefix configuration
- [ ] 6.3 Document 429 quota vs rate-limit handling
- [ ] 6.4 Document 529 classification
- [ ] 6.5 Document receipt provider identity fields
- [ ] 6.6 Include exact smoke test script reference

## 7. Smoke Test Script

- [ ] 7.1 Create `scripts/smoke_codiv.py`
- [ ] 7.2 Check for `CODIV_API_KEY`, exit 2 if missing
- [ ] 7.3 Implement direct Codiv API test (when key present)
- [ ] 7.4 Implement OmniRoute-mediated test (when key present)
- [ ] 7.5 Verify script NOT run in CI
- [ ] 7.6 Test: `python scripts/smoke_codiv.py` exits 2 without CODIV_API_KEY

## 8. Validation

- [ ] 8.1 Run new hermetic tests: verify 0 failed
- [ ] 8.2 Run full test suite: verify 0 failed
- [ ] 8.3 Run `mypy --strict verdict`: verify count >= 220 files
- [ ] 8.4 Run `ruff check`: verify 0 errors
- [ ] 8.5 Run `ruff format --check`: verify 0 changes needed
- [ ] 8.6 Run `verdict openspec admit bod-198-codiv-candidate`: verify exit 0
- [ ] 8.7 Stage all changes: `git add -A && git status --short`
- [ ] 8.8 Verify `git status --short` is empty before commit

## 9. Commit and PR

- [ ] 9.1 Commit with message file: `git commit -F /tmp/bod198-commit.msg`
- [ ] 9.2 Verify `git show --stat HEAD` has no stray files
- [ ] 9.3 Push branch to remote
- [ ] 9.4 Create PR with title: "feat(BOD-198): Codiv optional OmniRoute candidate: identity, gates, 429/529 classes, receipt identity"
- [ ] 9.5 Capture PR number and head SHA (`git rev-parse HEAD`)
- [ ] 9.6 Verify `git status --short` is empty (no uncommitted changes)
