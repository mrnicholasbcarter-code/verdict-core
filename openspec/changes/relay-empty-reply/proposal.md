# relay-empty-reply: treat finish_reason "length" / empty content as a failed attempt

## Why

The hourly acceptance `serve` check began failing consistently from 2026-11-22T18:00Z.
The relay POSTed to `antigravity/gemini-3.5-flash-lite` with `max_tokens: 16` and an
~18.5k-token context pack. The model consumed all 13 output tokens on internal reasoning
and returned HTTP 200 with `finish_reason: "length"` and `content: ""`. Offline
reproduction (2026-11-23) confirmed the shape 3/3 with the EXACT forwarded payload.

The relay loop breaks on `result.status_code < 400` without checking message content:

```python
if result.status_code < 400 or not (inband_refusal or retryable_response_status(...)):
    break
```

The relay recorded the attempt as `outcome: success`, returned the empty 200 to the
client, and the acceptance check (`_serve_verdict`) correctly detected `content is None`
and failed. The `serve-receipts.db` confirmed `terminal_outcome: success` for all
failing runs — the relay and the acceptance check disagreed on the same response.

**Root cause**: the relay's break condition accepts any HTTP 200 regardless of whether
the upstream produced usable content.

## What Changes

- `verdict/orchestration/provider_catalog.py`: add `is_truncated_empty_reply`. A
  completion body matches when every choice in `choices` has `finish_reason == "length"`,
  the message content is absent or empty, and the message carries no tool call. A partial
  answer (non-empty content), a tool call response, or any choice that is not a Mapping
  makes the function return False.
- `verdict/api.py`: after the existing `inband_refusal` block, add a parallel
  `truncated_empty` block. A matching buffered 200 is projected to 422, the loop
  continues to the next admitted candidate. `previous_refused` is extended to include
  `truncated_empty` (idempotency relaxed). `attempts_used` records `failure_class:
  "truncated_empty_reply"`. A synthetic `_truncated_empty_body(surface)` is returned
  if all candidates are exhausted. The 422 projection falls outside the feedback-write
  gate (`== 402 or >= 500`), so no route health negative is written — this is a
  prompt-budget event, not a route availability event.
- `verdict/acceptance.py`: raises the serve-check probe from `max_tokens: 16` to
  `max_tokens: 256` (named constant `_SERVE_PROBE_MAX_TOKENS`). With the original 16
  the relay fix catches the truncation but the same error can still occur on reasoning
  models. 256 gives enough headroom for a one-word reply after reasoning overhead.

## Capabilities

### Modified Capabilities

- **relay**: buffered HTTP 200 responses with `finish_reason: "length"` and no content
  are now treated as failed attempts rather than successes. The relay retries the next
  admitted candidate and fails closed if all are exhausted.
- **acceptance serve check**: the probe body now requests `max_tokens: 256` instead of 16.

## Impact

Affected files: `verdict/orchestration/provider_catalog.py`, `verdict/api.py`,
`verdict/acceptance.py`, `tests/test_relay_truncated_empty_reply.py` (new),
`tests/test_acceptance.py` (new test).

No change to admission, ranking, or route health feedback for well-formed 200 responses.
Partial answers (non-empty content, finish_reason "length") and tool-call responses pass
through unchanged. Streamed responses are not inspected.

## Evidence boundary

**KNOWN:** Offline reproduction 2026-11-23 confirmed 3/3: the EXACT forwarded body
(`build_cheap_path_context_pack` + `inject_context_pack` for surface "chat") sent to
`antigravity/gemini-3.5-flash-lite` returned HTTP 200, `finish_reason: "length"`,
`content: ""`, 13 reasoning tokens, 0 content tokens. **KNOWN:** `serve-receipts.db`
showed `terminal_outcome: success` for all acceptance-check rows in the failing window
(2026-11-22T18:00Z–2026-11-23). **KNOWN:** `is_inband_billing_refusal` already uses the
same 200→402 projection pattern and the same `_INBAND_REFUSAL_MAX_BYTES` size guard;
`truncated_empty` mirrors it exactly. **INFERRED:** raising `max_tokens` to 256 is
sufficient headroom; a one-word "OK" response requires only 1–3 tokens and 256 leaves
~243 tokens after a typical reasoning overhead of 10–13 tokens. **NOT AVAILABLE until a
live run:** confirmation that the hourly acceptance `serve` check passes on installed main
after the merge.

## Acceptance criteria

- `is_truncated_empty_reply` returns True for a body with `finish_reason: "length"` and
  absent or empty content, and False for partial answers, tool calls, non-Mapping choice
  entries, and any body where at least one choice has usable content.
- When the first admitted candidate returns a truncated-empty 200, the relay sends the
  request to the second candidate and returns its reply.
- The first attempt is recorded with `failure_class: "truncated_empty_reply"` and
  `outcome: "error"`; no route health negative is written.
- When all admitted candidates return truncated-empty, the relay returns an error with
  code `"truncated_empty_reply"` and does not forward a 200 with empty content.
- The serve-check probe sends `max_tokens: 256`; the test captures and asserts this.
- Tests are red on the unfixed code and green on the fix for all four relay scenarios
  plus the new serve-budget test.
