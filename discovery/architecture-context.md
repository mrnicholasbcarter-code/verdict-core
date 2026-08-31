# Architecture Context: Operational Routing Loop

## Reusable surfaces

- `StructuredPlanner` turns an objective into a validated `TaskSpec`, deterministic estimates, a `WorkflowPlan`, and bounded replan metadata.
- `OmniRouteAvailabilityAdapter` and `AvailabilityCache` normalize catalog/runtime observations and expose freshness.
- `EligibilityGate` is the single pre-ranking hard admission authority.
- `Policy` compiles deterministic route decisions and explanations.
- `gateway_adapters.py` already defines provider-neutral capabilities, route identity, request translation boundaries, and normalized failures.
- `autodev_run.py` plus `patch_executor.py` is the closest live product path: one bounded model patch, pre-apply path enforcement, independent command verification, usage capture, and durable receipts.
- `capture_source_state`, `ExecutionSession`, and `ContextPackCompiler` provide the reusable source, resume, and context primitives needed to make that path portable across models.
- `ReceiptStore` persists scoped, redacted, append-only evidence and supports replay/integrity checks.
- `SwarmDispatcher`/`SwarmTaskEnvelope` and complex verifier topology are valid later-phase capabilities, not Phase 1 dependencies.

## Boundary

The feature should extend the existing single-work-unit `autodev` path rather than
create another orchestration framework. Phase 1 adds only the packet, concrete-route
qualification/eligibility seam, minimal context compilation, restart-safe attempt
state, and one bounded primary fallback required by the live demonstration.
OmniRoute task routing and detection remain disabled.
