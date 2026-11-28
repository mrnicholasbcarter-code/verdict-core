# Spec Delta

## ADDED Requirements

### Requirement: A provider is out of quota only when every usable connection is

The AVAILABLE stage SHALL reject a route with `quota_low` only when every usable connection of the route's provider is measured and below the quota floor. A connection is usable when it is active and its test status is not a failed status. A provider with at least one usable connection that is measured at or above the floor SHALL NOT be rejected for quota. The cooldown of a `quota_low` rejection SHALL end at the earliest reset among the exhausted usable connections, and SHALL NOT be taken from a connection that is not usable.

#### Scenario: one exhausted account among accounts with quota

- **WHEN** a provider has three usable connections with quota left and one usable connection that is exhausted until a later reset
- **THEN** none of the provider's routes is rejected `quota_low` and no cooldown is taken from the exhausted connection

#### Scenario: every usable account is exhausted

- **WHEN** every usable connection of a provider is measured below the floor
- **THEN** the provider's routes are rejected `quota_low` with the cooldown at the earliest reset among those connections

#### Scenario: the only account with quota is not usable

- **WHEN** the one connection with quota left is inactive or has a failed test status, and every usable connection is exhausted
- **THEN** the provider's routes are rejected `quota_low`, with the cooldown from the usable connections' resets

### Requirement: Unknown quota stays unknown across connections

A connection whose quota for the route is not measured SHALL NOT count as having quota and SHALL NOT count as exhausted. A provider with an unmeasured usable connection and no measured connection at or above the floor SHALL NOT be rejected for quota, and SHALL report no quota percent.

#### Scenario: one unknown and one exhausted connection

- **WHEN** a provider has one usable connection with no quota reading and one usable connection that is exhausted
- **THEN** the provider's routes are not rejected `quota_low`, carry no quota cooldown, and report no quota percent

#### Scenario: no other stage is widened

- **WHEN** a route of a provider that is not rejected for quota fails another stage, such as the task gate
- **THEN** the route fails that stage with that stage's reason
