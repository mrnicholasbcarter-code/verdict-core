# Proposal: doctor capability gateway probe

## Why
verdict doctor reports gateway capabilities as DEGRADED because the capability report never probes the gateway. doctor_capability_report() runs run_bootstrap() without a probe_gateway argument. As a result, discover_providers() falls back to _default_gateway_probe. That function is a setup plan-mode stub that never contacts the gateway and always returns health_ok False with the reason "endpoint configured but not live-certified". So gateway.inventory and gateway.execute show DEGRADED with health null, even on machines whose OmniRoute gateway is healthy.

## What Changes
- doctor passes a real gateway probe into doctor_capability_report. doctor_capability_report gains a keyword argument probe_gateway: GatewayProbe | None = None and passes it to run_bootstrap(probe_gateway=...). When the argument is omitted, the default behaviour is unchanged. verdict/doctor_diagnostics.py gains _doctor_capability_gateway_probe(). For provider "gateway.omniroute" it resolves _cli_bootstrap() and calls _gateway_probe_for(bootstrap)(bootstrap.gateway_url). It returns reachable, health_ok, a redacted reason and a redacted endpoint. A BootstrapError returns its reason_code, and a missing gateway URL returns "no gateway url". Any other exception returns "probe error: <type>". Other providers return "not probed by doctor". _collect_doctor_diagnostics passes this probe to doctor_capability_report.

## Capabilities

### New Capabilities
- `doctor-capability-gateway-probe`: doctor probes the configured gateway for capability coverage, using the same authenticated probe as the doctor gateway lifecycle check.

### Modified Capabilities

## Impact
verdict/capability_bootstrap.py, verdict/doctor_diagnostics.py and one test file, tests/test_doctor_capability_gateway_probe.py. The tests use fake probes and monkeypatched bootstrap helpers, so they need no network access.

## Acceptance criteria
1. With a healthy fake probe, gateway capabilities are covered. gateway.inventory and gateway.execute report selected_provider_id "gateway.omniroute" and health "healthy".
2. With an unreachable fake probe, they stay not covered. The doctor probe maps an unreachable GatewayHealth to health_ok False with its detail as the reason. It maps a BootstrapError to its reason_code and any other provider to "not probed by doctor".

## Evidence boundary
- KNOWN: the default probe is a plan-mode stub that always returns health_ok False without contacting the gateway.
- INFERRED: a healthy gateway is reported DEGRADED today, because the capability report never receives a real probe result.
- NOT AVAILABLE: live doctor output after the fix; verification here uses fake probes only.
