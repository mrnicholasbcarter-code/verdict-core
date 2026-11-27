# Spec Delta

## ADDED Requirements

### Requirement: The screen probes routes that passive evidence admits

The scheduled screen SHALL take as candidates the free and unknown-capacity routes that the passive check admits, and the subscription routes in the planner's frontier band. It SHALL NOT probe a free or unknown-capacity route that the passive check rejects, and SHALL report such a route as skipped with the reason.

#### Scenario: route rejected by the passive check

- **WHEN** a free route has no live candidate row or its provider's breaker is open
- **THEN** the screen does not probe it and reports it as skipped

#### Scenario: unknown capacity

- **WHEN** a route of unknown capacity passes the passive check and holds no tool proof
- **THEN** it is a candidate on the same terms as a free route

### Requirement: The queue is ordered by expected value

The screen SHALL order its candidates by the product of an estimated chance of passing and a quality score. The chance SHALL be derived from the route prefix's own screening results of the last seven days and from passive evidence for the route. The quality score SHALL be Verdict's rating where one exists and otherwise the gateway's ranking of the provider. No such evidence SHALL make a route eligible, healthy or proven.

#### Scenario: a prefix that never passes

- **WHEN** one prefix has many recent probes and no pass and another has passed several times
- **THEN** candidates of the second prefix are probed before candidates of the first

#### Scenario: favourable evidence without a proof

- **WHEN** a route is healthy in the gateway's records, had successful traffic today, declares tool support and has no tool proof
- **THEN** it is early in the queue and is still not eligible for a worker node

### Requirement: A prefix that keeps failing is backed off

A prefix with at least the configured number of probes and no pass within seven days SHALL be skipped for the configured period and then given one probe. A pass SHALL end the back-off. Each skip SHALL be reported as `skipped_backoff`.

#### Scenario: back-off and return

- **WHEN** a prefix has 8 probes and no pass in seven days
- **THEN** its routes are skipped for 24 hours, then one is probed, and a pass restores the prefix

### Requirement: Providers the gateway rates avoid are not screened unless allowed

A route whose provider the gateway's free catalog rates `avoid` on terms of use SHALL NOT be screened and SHALL be reported as `skipped_terms`, unless the operator's allow list names the provider. No provider name SHALL be fixed in code.

#### Scenario: operator allows a provider

- **WHEN** the operator's allow list names a provider that the catalog rates `avoid`
- **THEN** its routes are candidates like any other

### Requirement: Every screen run reports the proven pool

Each screen run SHALL report the number of routes holding a tool proof by capacity class, the number of admitted routes without one, and the passes and probes of each prefix over seven days. `verdict doctor` SHALL show the proven-pool size.

#### Scenario: report after a run

- **WHEN** a screen run ends
- **THEN** its output contains the proven-pool counts, the backlog and the per-prefix record
