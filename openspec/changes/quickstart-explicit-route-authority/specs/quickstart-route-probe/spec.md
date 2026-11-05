## ADDED Requirements

### Requirement: Explicit-authority route probes SHALL fail closed

The route step SHALL accept only a zero child exit, first stdout line HTTP 200, and one JSON object with a non-empty decision string other than denied. Every other outcome SHALL exit non-zero without printing RESULT: PASS or QUICKSTART PASSED.

#### Scenario: Unsuccessful child result
- **WHEN** the child exits non-zero or returns a non-200, malformed or denied result
- **THEN** the quickstart fails without a success marker

### Requirement: Route authority input SHALL remain explicit

The probe SHALL read a JSON RouteRequest from VERDICT_QUICKSTART_ROUTE_REQUEST with non-empty task and execution_path_request object. Full validation and admission SHALL remain API-owned. The probe SHALL NOT manufacture candidates, eligibility, certification or decisions, and failures SHALL NOT dump input or credentials.

#### Scenario: Explicit authority input is absent or malformed
- **WHEN** the request file is absent, unreadable, invalid JSON, or lacks task or execution_path_request
- **THEN** the probe fails closed without inventing a candidate or exposing input contents

### Requirement: The probe SHALL retain server authentication and startup checks

The probe SHALL forward existing LLMGATE_AUTH_TOKEN bearer credentials without creating credentials or enabling anonymous access. It SHALL execute TestClient lifespan and use temporary HOME, VERDICT_HOME and receipts state.

#### Scenario: Real API authentication and schema remain authoritative
- **WHEN** middleware has no configured authentication
- **THEN** it returns 503; startup without authentication also refuses to initialize
- **WHEN** configured authentication receives missing, wrong, or valid bearer credentials
- **THEN** middleware returns 401, 403, or allows API validation respectively
- **WHEN** an authenticated API request omits task
- **THEN** it returns 422

#### Scenario: Explicit offers require server execution authority
- **WHEN** the request has valid schema, configured bearer authentication, and offers admitted by the server's evidence-backed execution-path flow
- **THEN** the in-process route-only API may return a selected decision
- **WHEN** execution authority is absent, a client supplies a decision, or offered routes are ineligible or not server-admitted
- **THEN** the route probe does not pass

#### Scenario: Offline integration evidence is not operational proof
- **WHEN** a test uses isolated snapshots, metadata and fresh fixture passports
- **THEN** actual API/admission/optimizer logic is exercised with network blocked, and the result is not represented as production admission or installed-main readiness
