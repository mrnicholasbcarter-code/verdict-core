# Spec Delta

## ADDED Requirements

### Requirement: Subscription and metered capacity come from connection billing facts

The capacity classifier SHALL decide `SUBSCRIPTION` and `METERED` from a connection billing fact, never from the connection's `authType` alone. The fact SHALL come from the pinned snapshot of OmniRoute's curated connection billing catalog or from an operator-declared overlay. An entry restricted to an `authType` SHALL win over a provider-wide entry, and an overlay entry SHALL win over the snapshot. Billing `unknown` and a missing entry SHALL NOT be facts. The deciding fact SHALL name the billing source.

#### Scenario: OAuth connection without a billing entry

- **WHEN** an OAuth connection's provider has no entry in the snapshot or the overlay, and no other fact applies
- **THEN** the route is `UNKNOWN` and its deciding fact is not `oauth_subscription`

#### Scenario: Plan-included API-key connection

- **WHEN** an API-key connection matches a curated entry with billing `subscription`
- **THEN** the route is `SUBSCRIPTION` with deciding fact `billing_catalog_subscription`

### Requirement: Overage behaviour is recorded per route

Each route's capacity evidence SHALL record the connection's overage behaviour as `hard-stop`, `meters-to-paid` or `unknown`. A `SUBSCRIPTION` route whose overage is not `hard-stop` SHALL be treated as `METERED` while its quota window is reported spent.

#### Scenario: Subscription that bills past its window

- **WHEN** a subscription route with overage `meters-to-paid` reports its quota window spent
- **THEN** selection treats it as `METERED` until the window resets

### Requirement: Free supply records its free type

Free facts read from the gateway's free-tier summary SHALL record `freeType` and `hardStopGuaranteed` for each route. Free-supply accounting SHALL report `one-time-initial` routes apart from recurring free supply, and a `discontinued` entry SHALL NOT be a free fact.

#### Scenario: One-time credit

- **WHEN** a route's free-tier entry has `freeType` `one-time-initial`
- **THEN** the route can be `FREE`, and the free-supply report lists it under one-time credit, not recurring supply

### Requirement: A label never decides capacity

The classifier SHALL NOT treat text in a connection's `plan_label`, `providerSpecificData.tier` or `providerSpecificData.plan` as a free fact.

#### Scenario: Plan label containing "free"

- **WHEN** a connection's plan label contains the word "free" and no other fact applies
- **THEN** the route is not `FREE` on that label

### Requirement: Billing snapshot drift is reported

`verdict doctor` SHALL compare the snapshot's recorded gateway version and source-file hash with the installed gateway and report a difference as drift.

#### Scenario: Gateway upgraded

- **WHEN** the installed gateway version differs from the snapshot's version
- **THEN** doctor reports billing-catalog drift and names both versions

### Requirement: Read-only connection capacity listing

`verdict discovery connections` SHALL list every connection with its capacity class, deciding fact, overage and fact source, and SHALL make no model calls.

#### Scenario: Listing on a live gateway

- **WHEN** the operator runs `verdict discovery connections`
- **THEN** each connection is listed once with class, deciding fact, overage and source, and no completion request is sent
