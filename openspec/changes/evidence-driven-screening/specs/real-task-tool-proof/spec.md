# Spec Delta

## ADDED Requirements

### Requirement: A verified real task creates or renews a tool proof

When a node that required tools is VALIDATED, the runtime SHALL record session-grade tool evidence for the attempt's exact route if, and only if, the executor was a live harness worker, the served model was verified against the assigned route, and the run's verification command passed. The evidence SHALL have the lifetime and meaning of an imported real-task session. It SHALL NOT be recorded for an alias of the route, from passive traffic, or from anything the worker reported about itself.

#### Scenario: tool-using node passes on a live worker

- **WHEN** a tool-requiring node is VALIDATED on a live worker whose served model was verified
- **THEN** the route holds a tool proof afterwards and the ladder accepts it for a worker node

#### Scenario: conditions not met

- **WHEN** the node did not require tools, or the executor was fault-injected, or the served model was not verified, or verification failed
- **THEN** no tool proof is recorded and the route's evidence is what it was

#### Scenario: passive traffic

- **WHEN** the gateway reports successful requests on a route that no controller verified
- **THEN** no tool proof is recorded
