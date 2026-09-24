// A finalized empty assistant turn must never look like successful silence.
// Provider diagnostics stay error/aborted; worker validation still rejects them.
export default function verdictTerminal(pi) {
  pi.on('message_end', async (event) => {
    const message = event.message;
    if (!message || message.role !== 'assistant') return;
    const blocks = Array.isArray(message.content) ? message.content : [];
    if (blocks.some((block) => block.type === 'toolCall')) return;
    if (blocks.some((block) => block.type === 'text' && block.text?.trim())) return;
    const diagnostic = message.errorMessage || `empty assistant output (stopReason=${message.stopReason})`;
    return {
      message: {
        ...message,
        // Do not convert provider failure into stop/success.
        stopReason: message.stopReason === 'aborted' ? 'aborted' : 'error',
        errorMessage: diagnostic,
        content: [...blocks, { type: 'text', text:
          `FAIL_CLOSED: ${diagnostic}. Inspect the owned worker operation events and outcome; `
          + 'if no outcome exists, resume the same operation through verdict-dispatch. '
          + 'Do not change the controller model to a worker route.' }],
      },
    };
  });
}
