# Design

## Context
The doctor capability report uses a stub probe. doctor_capability_report() calls run_bootstrap() with no probe_gateway, so discover_providers() uses _default_gateway_probe. That function always reports health_ok False. Doctor already has a real authenticated gateway probe, built from _cli_bootstrap() and _gateway_probe_for(bootstrap) and used by _doctor_gateway_lifecycle. The capability report does not use it.

## Goals / Non-Goals
Goal: probe the gateway. The capability report should reflect the real gateway health result that doctor already computes. Non-goal: change run_bootstrap. run_bootstrap, discover_providers and the default plan-mode probe keep their current behaviour for every caller that does not pass a probe.

## Decisions
Pass a probe callable through doctor_capability_report. The function gains a keyword-only-style argument probe_gateway: GatewayProbe | None = None and forwards it to run_bootstrap(probe_gateway=...). _doctor_capability_gateway_probe() in verdict/doctor_diagnostics.py returns a callable that takes a provider_id. For "gateway.omniroute" it resolves the bootstrap and calls the existing gateway probe. It returns a dict with reachable, health_ok (equal to reachable), a reason, and the endpoint redacted with redact_url. The reason is the health detail, or "gateway healthy" / "gateway did not answer" when there is no detail. A BootstrapError maps to its reason_code. A missing gateway URL maps to "no gateway url". Any other exception maps to "probe error: <type>". Other provider ids map to "not probed by doctor". _collect_doctor_diagnostics wires the probe in.

## Risks / Trade-offs
A probe error must never crash doctor. Every failure path returns reachable False and health_ok False with a short reason, so the capability stays not covered instead of raising. The trade-off is one extra gateway health request during doctor, reusing the probe that doctor already runs.

### Security / trust implications
The endpoint and detail are redacted; the key is never returned. The probe result contains only reachability, a reason string and the redacted endpoint. The API key and credentialed URLs are never printed or included in the report.

### Routing / context / memory implications
None: diagnostics only. Routing, admission and memory do not consume this probe.

### Migration / rollback
Revert the commit; no data migration. The new argument defaults to None, so other callers keep working unchanged.

### ADR impact
None: no architectural change. The change reuses an existing probe through an existing injection point.
