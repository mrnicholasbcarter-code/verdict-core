import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
const file = new URL('../.prime/agent/extensions/verdict-terminal.ts', import.meta.url);
const { default: register } = await import(`data:text/javascript;base64,${Buffer.from(readFileSync(file, 'utf8')).toString('base64')}`);
let handler;
register({ on: (name, callback) => { assert.equal(name, 'message_end'); handler = callback; } });
for (const stopReason of ['stop', 'error', 'aborted']) {
  test(`empty ${stopReason} cannot terminate silently`, async () => {
    const result = await handler({ message: { role: 'assistant', content: [], stopReason } });
    assert.match(result.message.content[0].text, /^FAIL_CLOSED/);
    assert.notEqual(result.message.stopReason, 'stop');
  });
}
test('provider status survives normalization', async () => {
  const result = await handler({ message: { role: 'assistant', content: [], stopReason: 'error', errorMessage: '429 antigravity' } });
  assert.equal(result.message.errorMessage, '429 antigravity');
  assert.match(result.message.content[0].text, /429 antigravity/);
});
test('nonempty response and tool turn unchanged', async () => {
  for (const content of [[{ type: 'text', text: 'ok' }], [{ type: 'toolCall', name: 'ipython' }]]) {
    assert.equal(await handler({ message: { role: 'assistant', content, stopReason: 'stop' } }), undefined);
  }
});

// Prime 0.9.6 overflow classifier (hue): overflow patterns win unless a rate-limit pattern matches.
const PRIME_OVERFLOW_PATTERNS = [/prompt is too long/i, /request_too_large/i, /input is too long for requested model/i, /exceeds the context window/i, /maximum context length is \d+ tokens/i];
const PRIME_RATE_LIMIT_PATTERNS = [/^(Throttling error|Service unavailable):/i, /rate limit/i, /too many requests/i];
const primeSeesOverflow = (text) => !PRIME_RATE_LIMIT_PATTERNS.some((p) => p.test(text)) && PRIME_OVERFLOW_PATTERNS.some((p) => p.test(text));
const KIRO = 'Input is too long. (reset after 56h 38m 47s)';
test('kiro overflow wording is unrecognised by Prime before normalization', () => {
  assert.equal(primeSeesOverflow(KIRO), false);
});
test('kiro overflow is normalized so Prime compacts instead of retrying', async () => {
  const result = await handler({ message: { role: 'assistant', content: [], stopReason: 'error', errorMessage: KIRO } });
  assert.equal(primeSeesOverflow(result.message.errorMessage), true);
  assert.match(result.message.errorMessage, /Input is too long\. \(reset after 56h/);
  assert.equal(result.message.stopReason, 'error');
  const text = result.message.content.at(-1).text;
  assert.match(text, /^CONTEXT_OVERFLOW/);
  assert.doesNotMatch(text, /verdict-dispatch/);
});
test('already-recognised overflow wording is not double-prefixed', async () => {
  const msg = '[400]: prompt is too long: 1091381 tokens > 1000000 maximum';
  const result = await handler({ message: { role: 'assistant', content: [], stopReason: 'error', errorMessage: msg } });
  assert.equal(result.message.errorMessage, msg);
  assert.match(result.message.content.at(-1).text, /^CONTEXT_OVERFLOW/);
});
test('overflow after partial output still normalizes errorMessage without new text', async () => {
  const content = [{ type: 'text', text: 'partial' }];
  const result = await handler({ message: { role: 'assistant', content, stopReason: 'error', errorMessage: KIRO } });
  assert.equal(primeSeesOverflow(result.message.errorMessage), true);
  assert.deepEqual(result.message.content, content);
});
test('rate limits are not reclassified as overflow', async () => {
  const result = await handler({ message: { role: 'assistant', content: [], stopReason: 'error', errorMessage: '429 rate limit (reset after 3m)' } });
  assert.equal(result.message.errorMessage, '429 rate limit (reset after 3m)');
  assert.match(result.message.content.at(-1).text, /^FAIL_CLOSED/);
});
