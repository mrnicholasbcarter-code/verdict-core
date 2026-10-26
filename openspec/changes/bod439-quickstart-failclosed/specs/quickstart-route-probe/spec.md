# Spec Delta

## Purpose

The clean-environment quickstart reports its route probe as success only when that probe returns a successful, non-denied routing decision.

## ADDED Requirements

### Requirement: A failed route probe SHALL NOT pass the quickstart
The quickstart route step SHALL treat the probe as success only when the child exits 0, the first output line is HTTP 200, and the remaining output is one JSON object whose `decision` is a non-empty string other than `denied`. A non-200 status, a non-zero child, malformed JSON, a non-object body, a missing or empty decision, or a decision of `denied` SHALL exit non-zero and SHALL print `RESULT: FAIL`. That path SHALL NOT print `RESULT: PASS` or `QUICKSTART PASSED`. A valid HTTP 200 response with a non-denied decision MAY continue. The API auth policy, probe payload, and process lifespan SHALL stay unchanged.

#### Scenario: Auth is not configured
- **WHEN** the route probe returns HTTP 503 and an error body
- **THEN** the quickstart exits non-zero and prints `RESULT: FAIL` without printing `RESULT: PASS`

#### Scenario: The route denies the request
- **WHEN** the route probe returns HTTP 200 and a JSON object whose `decision` is `denied`
- **THEN** the quickstart exits non-zero and does not print `RESULT: PASS`

#### Scenario: The route returns a decision
- **WHEN** the route probe returns HTTP 200 and a JSON object whose `decision` is a non-empty string other than `denied`
- **THEN** the quickstart may continue past the route step
