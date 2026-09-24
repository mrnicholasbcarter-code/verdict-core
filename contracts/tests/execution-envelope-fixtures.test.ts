import { describe, it, expect } from 'vitest';
import { parseContract } from '../src/index.js';
import { readFileSync, readdirSync } from 'fs';
import { join } from 'path';

describe('ExecutionEnvelope fixture parity', () => {
  const fixturesDir = join(__dirname, '../fixtures/execution-envelope/v1');
  const fixtureFiles = readdirSync(fixturesDir).filter(
    (f) => f.endsWith('.json') && f !== 'manifest.json'
  );

  fixtureFiles.forEach((filename) => {
    const shouldSucceed = filename !== 'unknown-field.json';
    
    it(`${shouldSucceed ? 'accepts' : 'rejects'} ${filename}`, () => {
      const fixturePath = join(fixturesDir, filename);
      const fixtureJson = readFileSync(fixturePath, 'utf8');
      const fixtureData = JSON.parse(fixtureJson);

      if (shouldSucceed) {
        // Should parse successfully
        expect(() => parseContract('execution_envelope', fixtureData)).not.toThrow();
        const parsed = parseContract('execution_envelope', fixtureData);
        expect(parsed).toBeDefined();
      } else {
        // Should throw on unknown field
        expect(() => parseContract('execution_envelope', fixtureData)).toThrow();
      }
    });
  });
});
