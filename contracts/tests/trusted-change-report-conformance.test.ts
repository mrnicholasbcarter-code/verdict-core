/**
 * Trusted Change Report — cross-runtime schema conformance (feature 002, T021).
 *
 * The Python canonical contract (`verdict/contracts.py` `TrustedChangeReport`)
 * and the TypeScript zod schema (`@bodanglin/verdict-contracts`
 * `trustedChangeReportSchema`) must accept and reject exactly the same
 * payloads. This suite consumes the shared Python fixtures under
 * `tests/fixtures/trusted_change_report/` and asserts the TypeScript runtime
 * agrees on every one: a payload Python accepts, TS accepts; a payload the
 * `.strict()` schema must reject, it rejects. This is the contract-conformance
 * leg of the release gate (gate 6, Python/TS parity).
 */
import { describe, it, expect } from 'vitest';
import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import {
  contractSchemas,
  parseContract,
  ContractValidationError,
  type TrustedChangeReport,
} from '../src/index.js';

const fixturesDir = fileURLToPath(
  new URL('../../tests/fixtures/trusted_change_report', import.meta.url),
);

async function loadReport(name: string): Promise<Record<string, unknown>> {
  return JSON.parse(await readFile(`${fixturesDir}/${name}`, 'utf8')) as Record<string, unknown>;
}

// Every fixture committed by the Python suite is a well-formed report: even the
// denied/tampered ones describe a valid report shape whose acceptance verdict
// or evidence flags fail the carrier's fail-closed rules — not the schema.
const validFixtures = [
  'report-accepted.json',
  'report-denied-failed-check.json',
  'report-denied-ineligible-route.json',
  'report-denied-missing-verification.json',
  'report-denied-out-of-scope.json',
  'report-denied-tampered-evidence.json',
  'report-denied-unbound-source.json',
  'report-redacted-export.json',
];

describe('trusted_change_report schema conformance (Python/TS parity)', () => {
  it('schema is registered under both the snake_case and PascalCase contract keys', () => {
    expect(contractSchemas.trusted_change_report).toBe(contractSchemas.TrustedChangeReport);
    expect(contractSchemas.trusted_change_report).toBeDefined();
  });

  it.each(validFixtures)('accepts %s exactly as the Python canonical contract does', async name => {
    const payload = await loadReport(name);
    const result = contractSchemas.trusted_change_report.safeParse(payload);
    expect(result.success).toBe(true);
    // parseContract must also accept the same payload. zod fills defaulted fields
    // the fixture may omit (metrics, acceptance.conditions, regression_observation,
    // source_state.commit_timestamp), so we assert the projected fields round-trip
    // rather than demanding byte-equality through defaults.
    const parsed = parseContract('trusted_change_report', payload) as Record<string, unknown>;
    for (const key of ['report_id', 'objective', 'task_type', 'work_unit_ids', 'route_decision']) {
      expect(parsed[key]).toEqual(payload[key]);
    }
  });

  it('parses every fixture file in the directory (guards against untested new fixtures)', async () => {
    const files = (await readdir(fixturesDir)).filter(f => f.endsWith('.json'));
    expect(files.sort()).toEqual(validFixtures.slice().sort());
    for (const f of files) {
      const payload = await loadReport(f);
      expect(contractSchemas.trusted_change_report.safeParse(payload).success).toBe(true);
    }
  });

  it('rejects an unknown top-level field (strict parity with Python)', async () => {
    const payload = await loadReport('report-accepted.json');
    const tampered = { ...payload, unexpected_field: true };
    const result = contractSchemas.trusted_change_report.safeParse(tampered);
    expect(result.success).toBe(false);
    expect(() => parseContract('trusted_change_report', tampered)).toThrow(ContractValidationError);
  });

  it('rejects an empty commit_sha in the nested source_state (required-bound parity)', async () => {
    const payload = await loadReport('report-accepted.json');
    const source = { ...(payload.source_state as Record<string, unknown>), commit_sha: '' };
    const tampered = { ...payload, source_state: source };
    const result = contractSchemas.trusted_change_report.safeParse(tampered);
    expect(result.success).toBe(false);
  });

  it('rejects an objective of the wrong type (boundary validation parity)', async () => {
    const payload = await loadReport('report-accepted.json');
    const tampered = { ...payload, objective: 42 };
    expect(contractSchemas.trusted_change_report.safeParse(tampered).success).toBe(false);
  });

  it('redacted export carries no producer-internal verification fields', async () => {
    const redacted = await loadReport('report-redacted-export.json');
    // The redacted fixture must validate as a full report shape.
    expect(contractSchemas.trusted_change_report.safeParse(redacted).success).toBe(true);
    const vrs = redacted.verification_results as Record<string, unknown>[];
    expect(vrs.length).toBeGreaterThan(0);
    for (const vr of vrs) {
      // export_redacted_report drops raw_output, command, runtime — these are
      // producer-internal and carry no decision value; they must not survive.
      expect(vr).not.toHaveProperty('raw_output');
      expect(vr).not.toHaveProperty('command');
      expect(vr).not.toHaveProperty('runtime');
    }
    // The computed acceptance decision is carried into the portable report.
    expect((redacted.acceptance as Record<string, unknown>).decision).toBe('accepted');
  });

  it('redacted export is leak-free (no provider credentials or API keys)', async () => {
    const redacted = await loadReport('report-redacted-export.json');
    const text = JSON.stringify(redacted);
    expect(text).not.toMatch(/sk-[a-z0-9_-]+/i);
    expect(text.toLowerCase()).not.toContain('api_key');
    expect(text).not.toContain('OPENAI_API_KEY');
    expect(text).not.toContain('ANTHROPIC_API_KEY');
  });
});
