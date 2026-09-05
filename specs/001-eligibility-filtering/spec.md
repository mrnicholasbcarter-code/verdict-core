# Feature Specification: Eligibility Filtering

**Feature Branch**: `337-eligibility-filtering`

**Created**: 2026-09-05

**Status**: Draft

**Input**: User description: "Run spec-kit specify for the next item we need to finish. Use the current project context to identify the next unfinished item, then produce or update the feature specification accordingly."

## Clarifications

### Session 2026-09-05

- Q: When a non-protected request only has best-available (uncertain) eligibility information, what determines whether it's still allowed to proceed? → A: A per-request-type flag/attribute (e.g. declared on the request or its route type)
- Q: What are the possible values for a candidate's eligibility status? → A: Eligible / Ineligible plus a numeric confidence score alongside the state
- Q: When a candidate becomes ineligible after evaluation but before the final routing decision, should eligibility be re-checked at selection time or treated as final? → A: Re-check eligibility immediately before final selection (authoritative, may add latency)

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Protect routed work from ineligible candidates (Priority: P1)

As a user requesting a route decision, I want only eligible candidates to be considered so that the selected target is valid and unsafe or unsupported options never win the decision.

**Why this priority**: This is the core safeguard for the routing flow and the main unfinished slice called out in the project context.

**Independent Test**: Can be tested by submitting a route request with a mix of eligible and ineligible candidates and confirming that only eligible candidates can be selected or returned.

**Acceptance Scenarios**:

1. **Given** a request that requires protected handling and at least one candidate is not eligible, **When** routing occurs, **Then** the ineligible candidate is excluded from consideration.
2. **Given** a request with no eligible candidates, **When** routing occurs, **Then** the request is not routed to an ineligible target.

---

### User Story 2 - Explain eligibility decisions (Priority: P2)

As a user reviewing a routing decision, I want to see which candidates were eligible, which were excluded, and why so that I can understand the result.

**Why this priority**: Transparency is the fastest way to build trust in the routing outcome and to support debugging and review.

**Independent Test**: Can be tested by requesting an explanation for a routed decision and verifying that the eligible set and exclusion reasons are visible.

**Acceptance Scenarios**:

1. **Given** a completed routing decision, **When** an explanation is requested, **Then** the response shows the full candidate set, the eligible subset, and reasons for each exclusion.
2. **Given** a request with a blocked candidate, **When** the explanation is requested, **Then** the reason for exclusion is included in the explanation.

---

### User Story 3 - Keep behavior consistent across route entry points (Priority: P3)

As a user invoking routing through different public surfaces, I want eligibility handling to be consistent so that the same request produces the same eligibility outcome everywhere.

**Why this priority**: Consistency across entry points prevents confusing mismatches and protects the user experience as routing expands.

**Independent Test**: Can be tested by sending the same request through each public route surface and comparing the eligibility outcome.

**Acceptance Scenarios**:

1. **Given** the same request sent through two supported route surfaces, **When** eligibility is evaluated, **Then** the eligible and excluded candidates match.
2. **Given** protected work with unavailable eligibility information, **When** routing occurs, **Then** the request is blocked rather than silently treated as eligible.

---

### Edge Cases

- No candidates meet eligibility requirements.
- Eligibility information is missing or unavailable for a candidate needed for protected work.
- Multiple candidates are eligible, but one becomes ineligible before the final decision.
- A routing explanation is requested after the original decision has already completed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST evaluate candidate eligibility before any ranking or selection step.
- **FR-002**: The system MUST exclude ineligible candidates from the final decision.
- **FR-003**: For protected work, the system MUST fail closed when eligibility cannot be established with confidence.
- **FR-004**: The system MUST keep eligibility behavior consistent across all public routing entry points.
- **FR-005**: The system MUST provide an explanation that includes the full candidate set, the eligible subset, and the reason each excluded candidate was excluded.
- **FR-006**: The system MUST ensure excluded candidates cannot reappear in the final chosen result, and MUST re-check eligibility immediately before final selection so a candidate that became ineligible after its earlier evaluation is not chosen.
- **FR-007**: The system SHOULD allow non-protected requests to continue with best-available eligibility information when the request's declared request-type flag/attribute permits it, and the decision explanation MUST make that posture visible.

### Key Entities *(include if feature involves data)*

- **Route Candidate**: A possible target for a request, with an eligibility state and a reason when excluded.
- **Eligibility Status**: The decision on whether a candidate may participate in routing — Eligible or Ineligible, each carrying a numeric confidence score from the evaluation source; a low-confidence score is what FR-003 treats as "cannot be established with confidence."
- **Routing Explanation**: The user-facing record that shows the candidate pool, the eligible subset, and exclusion reasons.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of protected requests with no eligible candidates are blocked rather than routed.
- **SC-002**: 100% of routing explanations include the eligible subset and exclusion reasons for excluded candidates.
- **SC-003**: The same request produces the same eligibility outcome across all public route surfaces in 100% of repeat checks.
- **SC-004**: Users can identify why a candidate was excluded from a routing decision in under 30 seconds during review.

## Assumptions

- The product already has a defined notion of protected versus non-protected requests.
- Candidate sources already exist and can be evaluated for eligibility.
- Existing routing review surfaces can present eligibility summaries without exposing sensitive internal details.
- The feature applies only to routing decisions and not to unrelated catalog browsing or administrative workflows.
