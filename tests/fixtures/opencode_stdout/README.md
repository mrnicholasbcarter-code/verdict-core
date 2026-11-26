# OpenCode stdout fixtures (captured tool output)

These files are the real standard output of the installed OpenCode, version 2.3.2
(`opencode v2.3.2`), run as `verdict orchestrate --executor opencode` runs it:

```
opencode run --standalone --auto --format json --archived --session <id> \
  --model verdict/fake-model --title "verdict <id>"      # the ask arrives on stdin
```

The model was a **fake endpoint**, not a model: a loopback HTTP server that answers every chat
request with a canned stream (a copy of `fake_model.py` with a few reply modes). No gateway, no
`verdict serve`, no provider and no paid or live model call was involved, and the run used a
temporary home and a temporary `OPENCODE_CONFIG_DIR`, written by the executor's own
`_write_attempt_config` and `_build_child_env`. The "key" in that config was a placeholder.
Captured on 2026-11-24. The session id (`ses_a1b2c3d4e5f60718293a`) was chosen by the capture
script. The other ids, and the timestamps, are the tool's own. Nothing was edited or trimmed after
capture. Every line is one JSON object, as printed. Exit codes and stderr are noted below.

| File | What the fake endpoint did | Exit | stderr |
|---|---|---|---|
| `v2_3_2_success.jsonl` | one reply, the text `ok`, usage 1 input and 1 output token | 0 | empty |
| `v2_3_2_multistep.jsonl` | step 1: the text `Running a command. ` and a `bash` tool call (usage 11/5); step 2: a reply streamed as six small deltas (usage 12/34) | 0 | empty |
| `v2_3_2_error.jsonl` | answered every request `429` with a `retry-after: 7` header | 1 | empty |
| `v2_3_2_error_503.jsonl` | answered every request `503`, no `retry-after` header | 1 | empty |
| `v2_3_2_error_403_serve_refusal.jsonl` | a loopback stand-in for `verdict serve` that answered every chat request `403` with serve's own refusal body for a bad assignment, `{"error": {"message": "authentication failed", "type": "authentication_error"}}` (captured 2026-11-25, #1325; no gateway, no model; the bearer was a signed assignment minted by the executor with a throwaway key) | 1 | empty |

What the captures show (and what the parser in `verdict/orchestration/executor_opencode.py`
relies on):

- The event `type` and `sessionID` are top-level. Everything else about the model's turn sits
  under `part`: the text (`part.text`), the stop reason (`part.reason`), the token counts
  (`part.tokens.input` and `part.tokens.output`) and the cost (`part.cost`).
- A text part is a **whole block**, not an increment. The six deltas of the streamed reply in
  `v2_3_2_multistep.jsonl` arrive as one `text` event holding the joined sentence. A turn that
  uses a tool prints one `text` event per block, so several events can belong to one run.
- `step_finish` is printed once per model step, with that step's own tokens. The run is the sum of
  its steps (`reason` is `tool-calls` for a step that ended in a tool call, `stop` for the last).
- `part.cost` is `0` for the custom provider (no pricing), even though the call was made.
- On an upstream failure OpenCode retries by itself, ten times, with a growing delay: it printed
  ten `retry` events (each carrying the same `error` object) over about 90 to 100 seconds, then one
  final `error` event, then exited 1. The final event is
  `{"type":"error", "error": {"type", "message", "status", "response": {"body"}, "retryAfterMs"}}`.
  `status` is the HTTP status, and `retryAfterMs` is the `retry-after` header in milliseconds
  (absent in the 503 capture). Nothing was printed on stderr.
- A `tool_use` event carries the tool's own `state`, under `part`; the parser does not read it.

Do not edit these files by hand: they are evidence of what the tool prints. To re-capture, run the
installed tool against a fake endpoint again, as above, and replace the files.
