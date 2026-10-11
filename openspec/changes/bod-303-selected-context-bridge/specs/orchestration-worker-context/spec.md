# Spec Delta: Selected worker context bridge

## ADDED Requirements

### Requirement: Runtime SHALL forward the exact selected route context facts

ContextPlan SHALL use only the selected verdict's context window and route identity. Missing windows SHALL remain unknown with a named conservative fallback. Reserves and shrinking byte/input budgets SHALL be explicit and SHALL narrow context, never admission.

#### Scenario: Multiple candidates have different windows

- **GIVEN** a selected small-window route and a larger unselected route
- **WHEN** DagRuntime hydrates its worker
- **THEN** the plan uses the selected route and window only.

#### Scenario: Missing window

- **WHEN** the selected window is missing
- **THEN** candidate_context_window remains null and budget_source is conservative_unknown_candidate.

### Requirement: Full worker prompt SHALL be checked before executor launch

Mandatory goal/objective/acceptance/ownership/verification/rules and retry feedback SHALL be counted before context compilation. Final UTF-8 bytes and offline token estimates SHALL be bounded by the exact request policy. Mandatory text SHALL NOT be silently truncated. Context non-fit SHALL block locally without provider cooldown or admission widening.

#### Scenario: Tiny selected window

- **WHEN** mandatory prompt and policy reserves cannot fit
- **THEN** the worker executor is not called and a local context_input_rejected failure is emitted.

#### Scenario: Overflow retry

- **WHEN** context overflow halves the byte allowance
- **THEN** the retry uses a new narrower input plan on the same selected route and records the actual retry pack sources.

### Requirement: Hydrate evidence SHALL describe actual selected pack units

Events SHALL record actual selected IDs, source URIs, excerpt digests and known spans, rendered-content digests and transform reasons, omissions, route/budget source, measured prompt bytes and explicitly estimated tokens. Missing tokenizer/revision SHALL remain unknown. Legacy callback results SHALL NOT cause independent prefix reads or fabricated sources.

#### Scenario: Unsafe sources and exclusions

- **GIVEN** safe excerpts, secret-bearing files and transcript files
- **WHEN** the production worker prompt is hydrated
- **THEN** only selected safe pack units appear in sources and omissions identify gaps.

#### Scenario: Legacy callback

- **WHEN** a scripted callback returns a string with no pack metadata
- **THEN** it keeps its compatible signature and sources is empty with unknown_legacy_callback status.
