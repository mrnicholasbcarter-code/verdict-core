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
