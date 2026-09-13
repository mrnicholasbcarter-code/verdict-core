import { describe, expect, it } from 'vitest';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

import { ContractValidationError, parseContract, serializeContract } from '../src/index.js';

const fixturesDir = fileURLToPath(new URL('../../test_fixtures/parity', import.meta.url));

type Fixture = Record<string, unknown>;

async function loadFixture(name: string): Promise<Fixture> {
  return JSON.parse(await readFile(`${fixturesDir}/${name}`, 'utf8')) as Fixture;
}

describe('BOD-12 shared contract parity fixtures', () => {
  it('round-trips valid routing, envelope, provider, and proof fixtures', async () => {
    const routingValid = await loadFixture('routing_decision_valid.json');
    const routingDefaults = await loadFixture('routing_decision_defaults.json');
    const routingMinimal = await loadFixture('routing_decision_minimal.json');
    const envelope = await loadFixture('envelope_explicit.json');
    const provider = await loadFixture('provider_receipt_valid.json');
    const proofValid = await loadFixture('proof_receipt_valid.json');
    const proofDenial = await loadFixture('proof_receipt_denial.json');

    expect(JSON.parse(serializeContract('routing_decision', routingValid))).toEqual(routingValid);
    expect(JSON.parse(serializeContract('routing_decision', routingDefaults))).toEqual(
      routingDefaults,
    );
    expect(JSON.parse(serializeContract('routing_decision', routingMinimal))).toEqual(
      routingDefaults,
    );
    expect(JSON.parse(serializeContract('execution_envelope', envelope))).toEqual(envelope);
    expect(JSON.parse(serializeContract('provider_receipt', provider))).toEqual(provider);
    expect(JSON.parse(serializeContract('proof_receipt', proofValid))).toEqual(proofValid);
    expect(JSON.parse(serializeContract('proof_receipt', proofDenial))).toEqual(proofDenial);
  });

  it('enforces strict unknown and missing field policy', async () => {
    const missingDetails = await loadFixture('provider_receipt_missing_details.json');
    const providerUnknown = await loadFixture('provider_receipt_unknown_field.json');
    const proofUnknown = await loadFixture('proof_receipt_unknown_field.json');

    expect(() => parseContract('provider_receipt', missingDetails)).toThrow(
      ContractValidationError,
    );
    expect(() => parseContract('provider_receipt', providerUnknown)).toThrow(
      ContractValidationError,
    );
    expect(() => parseContract('proof_receipt', proofUnknown)).toThrow(ContractValidationError);

    expect(() => parseContract('provider_receipt', missingDetails)).toThrowError(
      expect.objectContaining({ category: 'missing_field' }),
    );
    expect(() => parseContract('provider_receipt', providerUnknown)).toThrowError(
      expect.objectContaining({ category: 'unknown_field' }),
    );
    expect(() => parseContract('proof_receipt', proofUnknown)).toThrowError(
      expect.objectContaining({ category: 'unknown_field' }),
    );
  });

  it('applies the same explicit defaults to a minimal routing decision', async () => {
    const minimal = await loadFixture('routing_decision_minimal.json');
    const expected = await loadFixture('routing_decision_defaults.json');
    const normalized = parseContract('routing_decision', minimal);

    expect(normalized).toEqual(expected);
    expect(normalized.candidate_snapshot).toBeNull();
    expect(normalized.correlation_id).toBeNull();
    expect(normalized.request_id).toBeNull();
    expect(normalized.receipt).toBeNull();
  });
});
