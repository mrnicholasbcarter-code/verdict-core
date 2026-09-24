import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const file = new URL('../.prime/agent/extensions/verdict-context.ts', import.meta.url);

const load = async () => {
  const code = readFileSync(file, 'utf8');
  const { default: register } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
  const handlers = {};
  const entries = [];
  register({ on: (name, callback) => { handlers[name] = callback; },
    appendEntry: (name, value) => entries.push({name, value}) });
  return {handlers, entries};
};
test('context pressure persists guidance without aborting controller', async () => {
  const {handlers, entries} = await load();
  const ctx = {
    getContextUsage: () => ({ tokens: 130000, percent: 26 }),
    compact: () => { throw new Error('unsafe aborting API called'); },
  };
  await handlers.agent_end({}, ctx);
  await handlers.agent_end({}, ctx);
  assert.equal(entries.length, 1);
  assert.match(entries[0].value.instructions, /checkpoint/);
  assert.match(entries[0].value.instructions, /lease generation/);
});
test('neither turn_end abort nor fake compaction summary is installed', async () => {
  const {handlers} = await load();
  assert.equal(handlers.turn_end, undefined);
  assert.equal(handlers.session_before_compact, undefined);
});
