"""Shared invalid-envelope fixture manifest for cross-runtime parity (NOD-002).

Each entry documents one ``ExecutionEnvelope`` construction invariant enforced
by ``verdict.contracts`` (Python, source of truth) and mirrored by the Zod
schema in ``@bodanglin/verdict-contracts`` (``contracts/src/index.ts``).

The JSON fixtures live in ``test_fixtures/envelopes/`` and are copied verbatim
to ``verdict-node/test_fixtures/envelopes/``. Both runtimes must produce the
same accept/reject verdict for every fixture; error *messages* are
runtime-specific (Python ``ContractValidationError`` text vs. Zod issue text),
so parity is asserted on the verdict plus the Python error substring.

Consumed by ``tests/test_envelope_parity.py`` and mirrored by
``verdict-node/tests/contract-parity.test.ts``.
"""

from __future__ import annotations

# fixture file -> (invariant description, expected Python error substring)
INVALID_ENVELOPE_FIXTURES: dict[str, tuple[str, str]] = {
    "invalid_missing_task_spec.json": (
        "task_spec is required (no default)",
        "missing required field(s): task_spec",
    ),
    "invalid_wrong_type_task_spec.json": (
        "task_spec must be a TaskSpec object, not a scalar",
        "task_spec has invalid type",
    ),
    "invalid_task_spec_missing_objective.json": (
        "nested TaskSpec requires objective",
        "missing required field(s): objective",
    ),
    "invalid_task_spec_empty_objective.json": (
        "nested TaskSpec objective must not be blank",
        "objective must not be empty",
    ),
    "invalid_empty_policy_digest.json": (
        "policy_digest must be a non-empty string",
        "policy_digest must be a non-empty string",
    ),
    "invalid_wrong_type_allowed_capabilities.json": (
        "allowed_capabilities must be an array of strings",
        "allowed_capabilities must be an array",
    ),
    "invalid_empty_allowed_capability_item.json": (
        "allowed_capabilities items must be non-empty strings",
        "allowed_capabilities must contain non-empty strings",
    ),
    "invalid_wrong_type_execution_constraints.json": (
        "execution_constraints must be a JSON object",
        "execution_constraints must be an object",
    ),
    "invalid_wrong_type_verification_requirements.json": (
        "verification_requirements must be a VerificationPlan object",
        "verification_requirements has invalid type",
    ),
    "invalid_empty_evidence_id_item.json": (
        "evidence_ids items must be non-empty strings",
        "evidence_ids must contain non-empty strings",
    ),
    "invalid_unknown_field.json": (
        "unknown top-level fields are rejected (strict schema)",
        "unknown field(s): unknown_field",
    ),
}

# Fixtures that both runtimes ACCEPT: empty arrays are valid for
# allowed_capabilities and evidence_ids (only their items must be non-empty).
ACCEPTED_EMPTY_ARRAY_FIXTURES: tuple[str, ...] = (
    "invalid_empty_allowed_capabilities.json",
    "invalid_empty_evidence_ids.json",
)

VALID_FIXTURE = "valid_envelope.json"
