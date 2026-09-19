import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const file = new URL('../.prime/agent/extensions/verdict-context.ts', import.meta.url);

const load = async () => {
  const code = readFileSync(file, 'utf8');
  const { default: register } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);
  const handlers = {};
  register({ on: (name, callback) => { handlers[name] = callback; } });
  return handlers;
};

test('context budget compacts early and does not reschedule while pending', async () => {
  const handlers = await load();
  let calls = 0;
  let options;
  const ctx = {
    cwd: '/tmp',
    getContextUsage: () => ({ tokens: 130000, percent: 26 }),
    compact: (opts) => { calls++; options = opts; },
  };
  await handlers.turn_end({}, ctx);
  await handlers.turn_end({}, ctx);
  assert.equal(calls, 1);
  assert.match(options.customInstructions, /checkpoint/);
  assert.match(options.customInstructions, /lease generation/);
  options.onComplete();
  ctx.getContextUsage = () => ({ tokens: 15000, percent: 5 });
  await handlers.turn_end({}, ctx);
  assert.equal(calls, 1);
});

test('compaction boundary returns the installed Prime compaction contract', async () => {
  const handlers = await load();
  assert.equal(typeof handlers.session_before_compact, 'function');
  const preparation = { firstKeptEntryId: 'entry-1', tokensBefore: 130000 };
  const result = await handlers.session_before_compact(
    { preparation, customInstructions: 'focus on proofs' }, {},
  );
  assert.equal(result.cancel, undefined);
  assert.equal(result.compaction.firstKeptEntryId, 'entry-1');
  assert.equal(result.compaction.tokensBefore, 130000);
  assert.match(result.compaction.summary, /checkpoint\.json/);
  assert.match(result.compaction.summary, /lease generation/);
  assert.match(result.compaction.summary, /focus on proofs/);
  assert.equal(result.compaction.details.bounded, true);
  assert.equal(result.compaction.details.semantic_event, 'before_compact');
  assert.equal(result.compaction.details.contract, 'verdict.compaction');
  assert.match(result.compaction.summary, /context_pressure_checkpoint/);
  assert.equal((await handlers.session_before_compact({}, {})), undefined);
});
