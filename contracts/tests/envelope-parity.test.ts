/**
 * ExecutionEnvelope cross-runtime parity suite (NOD-002, ADR-025).
 *
 * Validates the shared invalid-envelope fixtures from
 * `test_fixtures/envelopes/` against the canonical Zod schema. The Python
 * side runs the same fixtures in `tests/test_envelope_parity.py`; both
 * suites must agree on every fixture's accept/reject verdict.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseContract, ContractValidationError } from '../src/index.js';

const FIXTURES_DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  'test_fixtures',
  'envelopes',
);

function loadFixture(name: string): unknown {
  return JSON.parse(readFileSync(join(FIXTURES_DIR, name), 'utf-8'));
}

// Must match tests/fixtures/invalid_envelopes.py (Python manifest).
const INVALID_FIXTURES = [
  'invalid_missing_task_spec.json',
  'invalid_wrong_type_task_spec.json',
  'invalid_task_spec_missing_objective.json',
  'invalid_task_spec_empty_objective.json',
  'invalid_empty_policy_digest.json',
  'invalid_wrong_type_allowed_capabilities.json',
  'invalid_empty_allowed_capability_item.json',
  'invalid_wrong_type_execution_constraints.json',
  'invalid_wrong_type_verification_requirements.json',
  'invalid_empty_evidence_id_item.json',
  'invalid_unknown_field.json',
];

// Both runtimes accept empty arrays; only array items must be non-empty.
const ACCEPTED_EMPTY_ARRAY_FIXTURES = [
  'invalid_empty_allowed_capabilities.json',
  'invalid_empty_evidence_ids.json',
];

const VALID_FIXTURE = 'valid_envelope.json';

describe('ExecutionEnvelope fixture parity', () => {
  it('covers every fixture on disk', () => {
    const onDisk = readdirSync(FIXTURES_DIR)
      .filter((name) => name.endsWith('.json'))
      .sort();
    const documented = [...INVALID_FIXTURES, ...ACCEPTED_EMPTY_ARRAY_FIXTURES, VALID_FIXTURE].sort();
    expect(onDisk).toEqual(documented);
  });

  it(`accepts ${VALID_FIXTURE}`, () => {
    expect(() => parseContract('execution_envelope', loadFixture(VALID_FIXTURE))).not.toThrow();
  });

  for (const fixture of INVALID_FIXTURES) {
    it(`rejects ${fixture}`, () => {
      expect(() => parseContract('execution_envelope', loadFixture(fixture))).toThrow(
        ContractValidationError,
      );
    });
  }

  for (const fixture of ACCEPTED_EMPTY_ARRAY_FIXTURES) {
    it(`accepts ${fixture} (empty arrays are valid)`, () => {
      expect(() => parseContract('execution_envelope', loadFixture(fixture))).not.toThrow();
    });
  }
});
