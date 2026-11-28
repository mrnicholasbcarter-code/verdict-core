# Spec Delta

## ADDED Requirements

### Requirement: The screen probes each model once

The scheduled screen SHALL treat catalog ids with the same `owned_by` and `root` as one model. It SHALL choose the id to probe by a fixed rule, so that the same id is chosen in every run, and SHALL NOT probe a model while either of its ids holds a fresh tool proof. A tool proof SHALL stay with the exact id that earned it.

#### Scenario: one model under two ids

- **WHEN** the catalog lists a model under its provider's alias and under its provider's full id
- **THEN** the screen probes the same one of the two ids in every run and reports the other as skipped

#### Scenario: the other id is not proven

- **WHEN** one id of a model holds a fresh tool proof
- **THEN** the model is not probed again and its other id is still not eligible for a worker node

### Requirement: The screen does not probe routes that cannot pass

The screen SHALL NOT probe a route whose catalog row does not declare tool support (a row without `tool_calling: true`), and SHALL report it as `skipped_no_tools`. A route with no catalog row is not skipped for that reason, and neither is a route with a pending rescreen request: a request names that exact route and only a probe consumes it. A route of unknown capacity SHALL be a candidate only when the passive check admits it, and then on the same terms as a free route.

#### Scenario: no declared tool support

- **WHEN** a free route's catalog row does not declare tool calling
- **THEN** the screen does not probe it and reports `skipped_no_tools`

#### Scenario: a rescreen request on a route without declared tool support

- **WHEN** a route whose catalog row does not declare tool calling has a pending rescreen request
- **THEN** the screen probes it once and the probe consumes the request

#### Scenario: unknown capacity

- **WHEN** a route of unknown capacity is admitted by the passive check, declares tool support and holds no tool proof
- **THEN** it is a candidate on the same terms as a free route

### Requirement: A provider that keeps failing is backed off

The screen SHALL keep, per provider, the probes and passes of the last seven days. A provider with at least the configured number of probes and no pass SHALL be skipped for the configured period and then given one probe. A pass SHALL end the back-off. Each skip SHALL be reported as `skipped_backoff`. A pass, a tool failure and the provider's answer that it does not serve the model SHALL count as a probe. A refusal by the gateway before any upstream call, a rate limit, a timeout and an upstream server error SHALL NOT.

#### Scenario: back-off and return

- **WHEN** a provider has 8 probes and no pass in seven days
- **THEN** its routes are skipped for 24 hours, then one is probed, and a pass restores the provider

#### Scenario: the gateway refused

- **WHEN** eight requests for a provider's routes were refused by the gateway because its breaker was open
- **THEN** the provider's record is unchanged and it is not backed off

### Requirement: The queue is ordered by the provider's record

The screen SHALL probe, in this order: routes the passive check admits; other routes of a provider with at least one pass in seven days; all other candidates. Inside each group it SHALL order by the provider's pass rate and then by Verdict's rating. While the last group is not empty it SHALL receive at least one probe in eight. A pending rescreen request SHALL NOT move a route ahead, and SHALL NOT let another id of the same model skip the route as its alias. No evidence used for ordering SHALL make a route eligible, healthy or proven.

#### Scenario: a provider that passes and one with no record

- **WHEN** one provider has passed several times this week and another has never been probed
- **THEN** the first provider's routes are probed first and the second still receives a probe in the run

#### Scenario: favourable evidence without a proof

- **WHEN** a route is admitted by the passive check, declares tool support, belongs to a provider that passes and has no tool proof
- **THEN** it is early in the queue and is still not eligible for a worker node

### Requirement: The operator's deny list excludes and a terms rating does not

A route whose provider is on the operator's deny list SHALL NOT be screened and SHALL be reported as `skipped_denied`. The deny list SHALL be keyed by provider id, SHALL be empty unless the operator sets it, and no provider name SHALL be fixed in code. Each screen run SHALL report, per provider, the terms rating that the gateway's free catalog gives it. The rating SHALL NOT by itself exclude a route.

#### Scenario: provider rated avoid, not denied

- **WHEN** a provider is rated `avoid` and is not on the deny list
- **THEN** its routes stay candidates and the report shows the rating

#### Scenario: operator denies a provider

- **WHEN** the operator's deny list names a provider
- **THEN** its routes are not probed and are reported as `skipped_denied`

### Requirement: Account-tied providers are probed within a limit

For a provider whose connections authenticate as a person (OAuth or cookie), the screen SHALL send at most the configured number of probes in one run.

#### Scenario: a sign-in provider with many unproven routes

- **WHEN** a provider whose connection signs in as a person has ten unproven routes and the limit is two
- **THEN** two are probed in the run and the rest are reported as skipped

### Requirement: Every screen run reports the proven pool

Each screen run SHALL report the number of routes holding a tool proof by capacity class, the number of candidate models without one, the passes and probes of each provider over seven days, and the providers in back-off. `verdict doctor` SHALL show the proven-pool size.

#### Scenario: report after a run

- **WHEN** a screen run ends
- **THEN** its output contains the proven-pool counts, the backlog and the per-provider record
