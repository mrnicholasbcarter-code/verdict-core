/**
 * Mutation corpus tests for ExecutionEnvelope v1: Python/Zod parity.
 *
 * For every case whose expected_verdict is REJECT_UNKNOWN, the Zod parse must FAIL.
 * For every other case, the Zod parse must SUCCEED (Zod doesn't know about DENY/EXPIRED/DIGEST_MISMATCH).
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { parseContract } from '../src/index.js';

interface MutationCase {
  id: string;
  base: string;
  override: Record<string, unknown>;
  expected_verdict: string;
}

interface MutationManifest {
  cases_digest: string;
  source_commit: string;
  evaluation_time: string;
  expected_policy_digest: string;
  description: string;
}

describe('ExecutionEnvelope mutation corpus', () => {
  const fixturesDir = join(__dirname, '../fixtures/execution-envelope');
  const v1Dir = join(fixturesDir, 'v1');
  const mutationsDir = join(fixturesDir, 'v1-mutations');

  const manifest: MutationManifest = JSON.parse(
    readFileSync(join(mutationsDir, 'manifest.json'), 'utf-8'),
  );
  const cases: MutationCase[] = JSON.parse(
    readFileSync(join(mutationsDir, 'cases.json'), 'utf-8'),
  );
  const baseFixture = JSON.parse(readFileSync(join(v1Dir, 'accepted.json'), 'utf-8'));

  it('manifest integrity', () => {
    const casesBytes = readFileSync(join(mutationsDir, 'cases.json'));
    const crypto = require('crypto');
    const actualDigest = `sha256:${crypto.createHash('sha256').update(casesBytes).digest('hex')}`;
    expect(actualDigest).toBe(manifest.cases_digest);
  });

  for (const testCase of cases) {
    it(`case ${testCase.id}: ${testCase.expected_verdict}`, () => {
      const mutatedEnvelope = { ...baseFixture, ...testCase.override };

      if (testCase.expected_verdict === 'REJECT_UNKNOWN') {
        // Zod must REJECT
        expect(() => parseContract('ExecutionEnvelope', mutatedEnvelope)).toThrow();
      } else {
        // Zod must ACCEPT (verdicts like DENY, EXPIRED, DIGEST_MISMATCH are runtime checks, not schema)
        expect(() => parseContract('ExecutionEnvelope', mutatedEnvelope)).not.toThrow();
      }
    });
  }
});
