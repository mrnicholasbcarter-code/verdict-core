# relay-empty-reply: treat finish_reason "length" / empty content as a failed attempt

## Why

The hourly acceptance `serve` check began failing consistently from 2026-11-22T18:00Z.
The relay POSTed to `antigravity/gemini-3.5-flash-lite` with `max_tokens: 16` and a
~18 k-token context pack. The model consumed all 13 output tokens on internal reasoning
and returned HTTP 200 with `finish_reason: "length"` and `content: ""`. Offline
reproduction (2026-11-23) confirmed the shape 3/3 with the EXACT forwarded payload.

The relay loop breaks on `result.status_code < 400` without checking message content:

```python
if result.status_code < 400 or not (inband_refusal or retryable_response_status(...)):
    break
```

The relay recorded the attempt as `outcome: success`, returned the empty 200 to the
client, and the acceptance check (`_serve_verdict`) correctly detected `content is None`
and failed.  The `serve-receipts.db` confirmed `terminal_outcome: success` for all
failing runs — the relay and the acceptance check disagreed on the same response.

**Root cause**: the relay's break condition accepts any HTTP 200 regardless of whether
the upstream produced usable content.

## What Changes

- `verdict/orchestration/provider_catalog.py`: add `is_truncated_empty_reply`. A
  completion body matches when `finish_reason == "length"` AND the message content is
  absent or empty AND the message carries no tool call. A partial answer (non-empty
  content, finish_reason "length") is not matched.
- `verdict/api.py`: after the existing `inband_refusal` block, add a parallel
  `truncated_empty` block. A matching buffered 200 is projected to 422, the loop
  continues to the next admitted candidate. `previous_refused` is extended to include
  `truncated_empty` (idempotency relaxed). `attempts_used` records `failure_class:
  "truncated_empty_reply"`. A synthetic `_truncated_empty_body(surface)` is returned
  if all candidates are exhausted. The 422 projection falls outside the feedback-write
  gate (`== 402 or >= 500`), so no route health negative is written — this is a
  prompt-budget event, not a route availability event.

## Safety

- Admission before ranking is unchanged: the loop only drops a 200 that produced no
  usable content and continues to an already-admitted candidate.
- Partial answers pass through: a non-empty content field with finish_reason "length"
  is never matched.
- Tool calls pass through: a message with tool_calls and no content is not matched.
- Attempts stay bounded: the same three-attempt limit and `attempt_gen` exhaust
  naturally with no extra looping.
- No health negative: `truncated_empty_reply` is not in `_RELAY_FEEDBACK_CATEGORIES`;
  the probe loop decides route health, the relay does not.
- Tests use stub transports. No provider is called.

## Impact

`verdict/orchestration/provider_catalog.py`, `verdict/api.py`,
`tests/test_relay_truncated_empty_reply.py`.

## Limits

- Streamed responses are not inspected: the relay only detects the truncation on
  buffered bodies.
- The underlying cause (context pack too large for the model's reasoning overhead at
  max_tokens=16) is noted but not fixed here; that is a separate sizing concern.
