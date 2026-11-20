# doctor-capability-gateway-probe Specification

## Purpose
Doctor probes the gateway for capability coverage, so a healthy OmniRoute gateway is not reported as DEGRADED by the plan-mode stub probe.

## Requirements

### Requirement: Doctor gateway probe
Doctor SHALL pass a gateway probe into the capability report so gateway capability coverage reflects the real redacted gateway health result.

#### Scenario: Healthy gateway
- **WHEN** the fake probe reports the gateway reachable
- **THEN** gateway capabilities are covered, with gateway.inventory and gateway.execute selecting provider gateway.omniroute with health healthy
