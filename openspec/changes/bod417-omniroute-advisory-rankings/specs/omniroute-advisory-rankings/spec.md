# Spec Delta

## Purpose

Adds an optional, advisory OmniRoute free-provider rankings signal to Verdict's
shared eligibility ladder. Rankings reorder only routes that already passed hard
admission; they never admit, entitle, or prove capability. This keeps gateway
ranking as a tie-break inside Verdict's authority (ADR-0001) behind a
provider-neutral evidence boundary (ADR-020).

## ADDED Requirements

### Requirement: Advisory rankings consumed only after hard admission
Verdict SHALL consult OmniRoute free-provider rankings only for routes that have already passed hard admission (authentication, entitlement, capability, quota, capacity, availability, authority, and risk). An excluded top-ranked route SHALL NOT be selected.

#### Scenario: Excluded top-ranked route cannot win
- **WHEN** a route is ranked highest by OmniRoute advisory but is excluded by admission (auth/payment/permission/capability/quota)
- **THEN** that route is not selected and the exclusion reason appears in the receipt

### Requirement: Exact provider/model matching only
Verdict SHALL apply an OmniRoute ranking score only to an exact provider/model route or an existing trusted canonical alias. Provider-best evidence SHALL NOT transfer to unrelated models of the same provider.

#### Scenario: Provider-best score does not transfer
- **WHEN** OmniRoute reports provider X's best-scored model is model A
- **THEN** model B of the same provider X receives no transferred score from model A

#### Scenario: Fuzzy prefix matching rejected
- **WHEN** a ranking entry uses a fuzzy version prefix (e.g. `gpt-6*` for `gpt-6.1-sol`)
- **THEN** the score is not applied to `gpt-6.1-sol` unless it is an exact or trusted alias match

### Requirement: Arena and AA metrics remain separate
Verdict SHALL NOT numerically mix Arena scores (0.4-0.98) with AA coding scores (0-100). They remain in separate source components.

#### Scenario: Metric separation enforced
- **WHEN** an Arena-scored route and an AA-coding-scored route are both admitted
- **THEN** their scores are not arithmetically compared or combined

### Requirement: Unknown scores stay unknown
Verdict SHALL NOT default a missing, stale, malformed, 404, timeout, or unmatched ranking score to zero. Unknown SHALL remain null/unknown.

#### Scenario: Missing ranking yields named omission
- **WHEN** the OmniRoute rankings endpoint returns no entry for an admitted route
- **THEN** the route retains baseline ordering and the omission reason is recorded in the receipt

#### Scenario: Stale/malformed/404/timeout yields baseline order
- **WHEN** the rankings response is missing, stale, malformed, HTTP 404, or times out
- **THEN** selection falls back to identical baseline ordering with a named omission; no hard dependency

### Requirement: Generic gateways are not probed
Verdict SHALL NOT query the OmniRoute-only rankings management endpoint on a generic OpenAI-compatible gateway. A non-OmniRoute gateway SHALL yield a named omission and baseline ordering.

#### Scenario: Generic gateway skipped
- **WHEN** selection runs against a generic OpenAI-compatible gateway
- **THEN** no rankings request is sent and the receipt records a `generic_gateway` omission with baseline ordering

### Requirement: Task-aware category flow
Verdict SHALL propagate coding/debugging/review task category from `verdict choose` into the shared selector with backwards-compatible default for other callers.

#### Scenario: Category flows to selector
- **WHEN** `verdict choose --task-class implementation --requires tools,code` runs
- **THEN** the coding category is conveyed into the eligibility ladder and rankings advisory

#### Scenario: Other callers keep default category
- **WHEN** a non-coding caller invokes selection
- **THEN** it defaults to coding/general category and existing behavior is unchanged

### Requirement: Receipt/provenance exposure
Verdict SHALL expose ranking category, source, observed timestamp, reliability/usage context, and omission reason in existing rank components/explanations without fabricating benchmark dates.

#### Scenario: Receipt exposes provenance
- **WHEN** rankings advisory is applied
- **THEN** the receipt shows category, source, observed timestamp, reliability context, and any omission reason

#### Scenario: Unknown is not zero-filled
- **WHEN** a ranking score is unavailable
- **THEN** the receipt records unknown/null rather than a fabricated zero or benchmark date
