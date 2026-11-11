# Proposal: the 1-token liveness probe never reads or writes agentic proof

## Why

At 12:00:34Z on 2026-11-11 the hourly acceptance run's ladder confirmation probe (`max_tokens: 1`,
"Return exactly: OK") cleared the tool proof of three routes. Captured bodies from those routes
(`antigravity/gemini-3-flash`, `groq/openai/gpt-oss-20b`, `opencode-go/beam-free`) are all HTTP 200
with a well-formed assistant message cut off by the token limit: `finish_reason: "length"`,
`content` empty or null, 0 or 1 completion tokens, identity headers matching the requested route,
no cache or compression anomaly. `_has_inference_output` requires non-empty text, so the probe
classed each reply `malformed_response`, and `HealthCache.record` clears `agentic_ok` after a
failed probe unless the category is one of timeout, upstream, rate limited, auth, payment or
permission. Chat liveness evidence erased unrelated capability proof (BOD-299 AC1, BOD-345).

## What Changes

- `openai_health_probe`: a reply is ALIVE for chat only when ALL hold: no truthy top-level
  `error`; exactly one choice with `finish_reason: "length"` (not `stop`); an assistant message
  whose `content` key is present and is null, empty or whitespace-only, with no truthy `refusal`,
  `tool_calls` or `function_call`; and the gateway's served identity verifies as the requested
  route (cache hits and other anomalies still void it). It is flagged in `error_message`
  (`empty_content`, plus `zero_output_tokens` when usage says 0). Every other shape stays
  `malformed_response`.
- `record_dispatch_observation(..., liveness_only=True)` and `HealthCache.record(...,
  keep_agentic=True, keep_tool_ok=True)`: a failed chat-liveness observation marks the route
  unhealthy for chat but carries the previous agentic fields and `tool_ok` over unchanged. The
  entry stays unhealthy, so every reader of `tool_ok` (each also requires a healthy, fresh or
  chat-ok entry; audited 2026-11-11) still skips the route. The ladder (`_record_health`) and the worker
  launch probe pass `liveness_only=True`. Every other caller keeps today's behaviour.
- `malformed_response` is NOT added to the global categories that preserve agentic proof.

## Impact

- Changed: `verdict/subagent_selection.py`, `verdict/admit_prove_confirm.py`,
  `verdict/orchestration/health_cache.py`, `verdict/orchestration/eligibility.py`,
  `verdict/worker_runtime.py`.
- New: `tests/test_liveness_probe_agentic_isolation.py`.
- No new model calls, no new cadence, no new dependency.

## Evidence boundary

KNOWN: the three captured response bodies and headers (BOD-299 comment, 2026-11-11 14:57Z).
NOT AVAILABLE: a measurement that acceptance stays green for 48 h; that needs changes A, C and D
live as well.
