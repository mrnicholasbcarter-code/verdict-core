# Proposal: Codiv as Optional OmniRoute Execution Candidate

## Why

Verdict currently has no path to use Codiv models through OmniRoute. Without provider identity tracking, distinct 429 quota vs rate-limit classification, and proper failure handling, Codiv routes cannot be safely integrated into the execution ecosystem.

**KNOWN**: BOD-198 (Linear issue f0def261) acceptance criteria require exact provider/model identity, standard eligibility gates, 429/529 failure classification, receipt identity tracking, and absence safety.

**KNOWN**: Live OmniRoute inventory currently has NO `codiv/` provider prefix. `diffusiongemma-26b-a4b-it` exists only under `nvidia/`, `bm/`, and `bluesminds/` providers (verified by controller).

**INFERRED**: Provider identity must use exact prefix matching (not model name matching) to prevent cross-provider confusion.

**NOT AVAILABLE**: Production Codiv latency and cost data (requires live CODIV_API_KEY and OmniRoute configuration).

## What Changes

- Add Codiv provider identity with exact prefix-based matching (default `"codiv"`)
- Extend `NormalizedFailureClass` for 429 quota exhaustion vs rate limiting distinction
- Add 529 provider overload classification (infrastructure pressure, not quality degradation)
- Extend `ExecutionReceipt` with intended/executed provider/model identity tracking
- Implement bounded Retry-After header parsing with malicious-value clamping (1s-300s)
- Add hermetic test fixtures for provider identity and failure classification
- Document hermetic vs operational split in `docs/providers/codiv.md`
- Add `scripts/smoke_codiv.py` smoke test (not run in CI, exits 2 without CODIV_API_KEY)

## Capabilities

### New Capabilities
- `providers/codiv-identity`: Codiv provider identification via exact prefix matching
- `failures/quota-rate-distinction`: Distinct 429 quota exhaustion vs rate limiting
- `failures/provider-overload`: 529 as infrastructure pressure (not model quality signal)
- `receipts/provider-identity`: Track intended vs executed provider/model with fallback detection

### Modified Capabilities
<!-- No existing capability requirements are changing -->

## Acceptance criteria

- [ ] Codiv routes identified by exact provider prefix match (not model name)
- [ ] Test: inventory with `nvidia/diffusiongemma-26b` only → 0 Codiv candidates
- [ ] 429 quota exhaustion → `QUOTA_EXHAUSTED` state, no retry loop
- [ ] 429 rate limit → `RATE_LIMITED` state with bounded cooldown from Retry-After
- [ ] Test: malicious Retry-After (negative, huge, garbage) → clamped to 1s-300s
- [ ] 529 → provider capacity/infrastructure pressure class (not quality degradation)
- [ ] Receipt captures `intended_provider`, `intended_model`, `executed_provider`, `executed_model`
- [ ] Receipt flags `provider_mismatch=True` when fallback occurred
- [ ] Missing `CODIV_API_KEY` → Codiv routes ineligible with reason, other routes unaffected
- [ ] Protected work + Codiv + paid route → not forced to Codiv without positive availability
- [ ] New tests pass, full suite 0 failed, mypy --strict >= 220 files
- [ ] `verdict openspec admit bod-198-codiv-candidate` exits 0
- [ ] `python scripts/smoke_codiv.py` exits 2 without CODIV_API_KEY
- [ ] PR opened with head SHA and empty `git status --short`

## Impact

Affected modules:
- `verdict/gateway_adapters.py` (extend `NormalizedFailureClass`)
- `verdict/autodev_routing.py` (429 response body parsing)
- `verdict/bounded_recovery.py` (Retry-After parsing and clamping)
- `verdict/orchestration/receipt.py` (add provider identity fields)
- `verdict/eligibility.py` (verify standard gates apply to Codiv)

New files:
- `docs/providers/codiv.md` (hermetic vs operational documentation)
- `scripts/smoke_codiv.py` (smoke test, not run in CI)
- Test fixtures for provider identity and failure classification

Not affected:
- Hard admission policy (Guardrail: no policy authority change)
- OpenJev System One (BOD-199, separate story)
- Other provider routes (absence safety requirement)
