// A finalized empty assistant turn must never look like successful silence.
// Provider diagnostics stay error/aborted; worker validation still rejects them.
//
// Context overflow: some providers word it in a way Prime's overflow classifier
// does not recognise (kiro: "Input is too long. (reset after 56h ...)"). Prime then
// retries the same oversized request as a transient error instead of compacting.
// Normalise those to Prime's recognised "prompt is too long" wording, keep the
// provider's original text, and say what actually happens next.
const OVERFLOW = [
  /input is too long/i, /prompt is too long/i, /context[_ ]length[_ ]exceeded/i,
  /maximum context length/i, /exceeds the context window/i, /too many tokens/i,
];
// Prime 0.9.6 treats these as overflow (subset of its classifier).
const PRIME_OVERFLOW = /prompt is too long|input is too long for requested model|exceeds the context window|maximum context length is \d+ tokens/i;

export const isContextOverflow = (text) => OVERFLOW.some((pattern) => pattern.test(text || ''));

export const normalizeOverflow = (errorMessage) => (
  PRIME_OVERFLOW.test(errorMessage) ? errorMessage : `prompt is too long: ${errorMessage}`
);

export default function verdictTerminal(pi) {
  pi.on('message_end', async (event) => {
    const message = event.message;
    if (!message || message.role !== 'assistant') return;
    const blocks = Array.isArray(message.content) ? message.content : [];
    const overflow = message.stopReason === 'error' && isContextOverflow(message.errorMessage);
    const errorMessage = overflow ? normalizeOverflow(message.errorMessage) : message.errorMessage;
    const hasOutput = blocks.some((block) => block.type === 'toolCall')
      || blocks.some((block) => block.type === 'text' && block.text?.trim());
    if (hasOutput) return overflow ? { message: { ...message, errorMessage } } : undefined;
    const diagnostic = errorMessage || `empty assistant output (stopReason=${message.stopReason})`;
    const guidance = overflow
      ? `CONTEXT_OVERFLOW: ${diagnostic}. The request exceeded the model's real input limit. `
        + 'Prime compacts and retries once; if that fails, run /compact before continuing. '
        + 'This is a context-size failure, not a worker or provider outage.'
      : `FAIL_CLOSED: ${diagnostic}. Inspect the owned worker operation events and outcome; `
        + 'if no outcome exists, resume the same operation through verdict-dispatch. '
        + 'Do not change the controller model to a worker route.';
    return {
      message: {
        ...message,
        // Do not convert provider failure into stop/success.
        stopReason: message.stopReason === 'aborted' ? 'aborted' : 'error',
        errorMessage: diagnostic,
        content: [...blocks, { type: 'text', text: guidance }],
      },
    };
  });
}
