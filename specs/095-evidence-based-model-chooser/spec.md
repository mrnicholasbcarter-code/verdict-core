# Feature Specification: Evidence-based model chooser (BOD-95)

**Feature Branch**: `feat/bod-95-model-chooser`
**Created**: 2026-09-13
**Status**: Draft
**Linear**: BOD-95
**Input**: Ship the smallest working vertical slice that chooses a currently usable execution target using existing Verdict eligibility, then expose it as `verdict choose` for Prime.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Ordinary implementation prefers free/zero-marginal (Priority: P1)

As an operator running ordinary coding work, I need Verdict to choose an eligible healthy free/zero-marginal target over premium so Prime can dispatch cheap capable workers first.

**Why this priority**: This is the P0 production behavior and the first proof case.

**Independent Test**: Fixture with healthy free worker vs healthy premium selects the free target for ordinary implementation.

**Acceptance Scenarios**:

1. **Given** a healthy free candidate and a healthy premium candidate, **When** `verdict choose --task-class implementation` runs, **Then** the selected identity is the free candidate and the receipt explains the resource-class reason.
2. **Given** the free candidate fails the capability gate, **When** the chooser runs, **Then** it falls through to subscription worker capacity before premium/metered capacity.
3. **Given** the free candidate is quota-exhausted or unhealthy, **When** the chooser runs, **Then** that candidate is excluded before ranking and never appears as selected.

### User Story 2 - Explicit selection and protected work (Priority: P1)

As an operator, I need explicit eligible model selection to win, ineligible explicit selection to fail closed, and protected architecture/final-review work to select an eligible premium-designated candidate.

**Why this priority**: Silent replacement and unprotected premium work are the main safety failures.

**Independent Test**: Three fixtures: explicit eligible, explicit ineligible, protected architecture.

**Acceptance Scenarios**:

1. **Given** an explicit eligible model, **When** choose runs, **Then** that candidate is selected and the receipt shows explicit selection beat ranking.
2. **Given** an explicit ineligible model, **When** choose runs, **Then** it fails closed with a named reason and does not silently switch.
3. **Given** task class `architecture` or `final-review`, **When** an eligible premium-designated candidate exists, **Then** that candidate is selected.

### User Story 3 - Deterministic receipt for Prime dispatch (Priority: P1)

As Prime, I need a versioned JSON receipt with selected gateway/provider/resource-pool/model, fallbacks, exclusions, unknown evidence, and ranker version so I can dispatch the exact selected model.

**Why this priority**: Prime must never inherit a model implicitly.

**Independent Test**: `verdict choose --json` over identical evidence is deterministic; two gateways with the same model ID remain distinct; unknown quota remains UNKNOWN.

**Acceptance Scenarios**:

1. **Given** identical candidate evidence, **When** choose is run twice, **Then** ranking and selected identity are identical.
2. **Given** two gateways serving the same model ID, **When** both are eligible, **Then** identities remain distinct in selected/fallback records.
3. **Given** missing quota/headroom, **When** a receipt is emitted, **Then** those fields remain UNKNOWN and are never promoted to 100%.
4. **Given** no eligible candidates, **When** choose runs, **Then** it returns a clear no-eligible-target result with exclusion reasons.

### Edge Cases

- UNKNOWN availability/quota/headroom is never treated as perfect.
- Quota-exhausted/unhealthy/ineligible candidates never reach the ranker.
- `EligibilityGate` remains the sole hard admission authority.
- Selection identity is gateway + provider + resource_pool + model, not model string alone.
- Brand prefixes are not ranking rules; resource class is config/evidence mapped.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Reuse `CandidateEvidence` + `EligibilityGate` + `select_eligible_route(..., ranker=...)` in `verdict.autodev_routing`. Do not create another router.
- **FR-002**: Add a production advisory ranker that ranks only gate-admitted candidates by resource class then existing task-fit/health/headroom evidence.
- **FR-003**: Resource classes are `free`, `subscription_worker`, `subscription_premium`, `metered`, `local`. Ordinary work order: free → subscription_worker → subscription_premium/metered last unless protected or cheaper classes are unqualified.
- **FR-004**: Protected classes are `architecture`, `orchestration`, `hard-debug`, `final-review`.
- **FR-005**: Add `verdict choose` with `--task-class`, `--requires`, `--model` (explicit), `--json`, and human explanation `selected because ...`.
- **FR-006**: JSON receipt includes: task class, protected flag, selected gateway/provider/resource-pool/model, evidence digest/freshness, ranked fallbacks, exclusions with exact reasons, ranking factors, unknown evidence fields, policy/ranker version.
- **FR-007**: Explicit eligible model wins over ranking and is visible in the receipt.
- **FR-008**: Explicit ineligible model fails closed; all-unavailable returns no-eligible-target.
- **FR-009**: Missing quota/headroom remains UNKNOWN.
- **FR-010**: One real Verdict task is dispatched using the chooser JSON and an explicit selected model.

### Key Entities

- **CandidateEvidence**: existing secret-free route evidence.
- **Route identity**: `gateway_id`, `provider`, `resource_pool`, `model_id`. `resource_pool` is a normalized class carried on the receipt; native `AdapterRouteIdentity` remains gateway/provider/model/route_id.
- **ChooseReceipt**: versioned machine contract for Prime.

### Assumptions

- Linear has no READY status; BOD-95 is treated as the operator-named P0 because it is incomplete, unblocked, Urgent, and `automation:eligible`.
- Live catalog probing is out of scope; tests may use fixture evidence.
- SONA, models.dev priors, Dagger, Phoenix, PWA, and sandboxing are out of scope.

## Success Criteria *(mandatory)*

- **SC-001**: All ten proof cases in BOD-95 pass with deterministic fixtures.
- **SC-002**: `verdict choose --json` is a stable versioned contract.
- **SC-003**: Prime can consume the JSON and launch one worker with the explicit selected model.
- **SC-004**: Existing EligibilityGate tests continue to pass; the ranker never admits excluded candidates.
