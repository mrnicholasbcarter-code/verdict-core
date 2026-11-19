# Proposal: the 1-token liveness probe accepts a truncated reply with no content key

## Why

On 2026-11-19 the ladder confirmation probe (`max_tokens: 1`, "Return exactly: OK") classed
`cerebras/gpt-oss-120b` as `malformed_response` twice (04:08Z and 05:35Z). In both certification
rehearsals of that day the admission receipt records the route as not admitted, first failed
stage `HEALTHY`, reason `malformed_response`, source `ladder_probe:openai_health_probe`. It was
the only independent reviewer candidate, so both rehearsals blocked.

The gateway call log holds both replies: HTTP 200, one choice, `finish_reason: "length"`,
1 completion token, no error, and an assistant message that holds only `role`. There is no
`content` key. The same route answered three tool-call probes and a normal chat correctly in the
minutes before. The existing rule accepts a truncated reply only when the `content` key is
present (null, empty or whitespace), so a route that omits the key is rejected for the shape of
an otherwise valid truncated completion.

The provider omits the key; the gateway does not drop it. In the same day's log the gateway
passes `content: ""` (antigravity, groq, cloudflare) and `content: null` (kilocode, nvidia)
through unchanged, and the gateway's response sanitizer copies `content` whenever the upstream
message has it. The key is also absent on this provider's tool-call replies.

## What Changes

- `_empty_but_alive_flags` in `verdict/subagent_selection.py`: a missing `content` key is treated
  like a null or empty one only when every existing condition holds unchanged (no top-level
  error, exactly one choice, `finish_reason: "length"`, role `assistant`, `refusal`, `tool_calls`
  and `function_call` each absent or null, served identity verified) and two more hold for this
  shape: `usage.completion_tokens` is an integer of 0 or more, and `reasoning` and
  `reasoning_content` are each absent, null or blank.
- The shape is reported under its own flag, `missing_content`, never as `empty_content`.
  `zero_output_tokens` is still added when usage reports 0.
- Not changed: `max_tokens`, the request, `finish_reason: "stop"` (still rejected), served
  identity checking, proof-voiding anomalies, the in-band billing refusal check, the result for
  any reply that has text, and the flags of the existing empty-content and null-content shapes.

## Why the usage condition

A message with no `content` key says nothing itself about the completion. The only other
statement in the body that a completion ran is the token count. Requiring a readable count keeps
"unknown is never healthy": a body with no usage, or a count that is null, a boolean, a float or
text, stays `malformed_response`. The present-key shapes keep their rule and need no usage,
because that rule is not changed here.

## Why the reasoning condition

A 1-token reply from a reasoning model can spend its token on reasoning. If `reasoning` or
`reasoning_content` holds text, the reply already counts as inference output and never reaches
this rule (unchanged; the same day's log has one such reply). If either field holds any other
non-null value (a list, an object, a number), the reply cannot be read as the captured shape and
stays `malformed_response`.

## Impact

- Changed: `verdict/subagent_selection.py`, `docs/guides/health-cache.md`,
  `tests/test_liveness_probe_agentic_isolation.py` (one comment).
- New: `tests/test_health_probe_missing_content.py`,
  `tests/fixtures/health_probe/missing-content-length.json`.
- Every caller of `openai_health_probe` gets the new result: the ladder confirmation
  (`verdict choose`, `verdict serve` `/v1/route`, `verdict orchestrate`, `verdict eligibility`,
  autodev, the routing view) and the worker launch probe. The relay path does not call it.
- No new model calls, no new cadence, no new dependency.

## Evidence boundary

KNOWN: the two logged replies and their shape (gateway call log, 2026-11-19); the two admission
receipts; one live probe on this branch at 11:07Z on 2026-11-19 that returned healthy with the
flag `missing_content`.
NOT KNOWN: whether other providers omit the key. Of the 40 one-token probes logged that day,
only this route did.
ARCHIVE ORDER: this delta modifies a requirement that the open change
`liveness-probe-no-agentic-state` adds. That change must be archived first; `openspec` refuses
to archive this one before the `liveness-probe` spec exists.
NOT CHANGED HERE: after a failed confirmation the admitted set records the route with no
`until`, so the route stays out for the rest of the run although the ladder's route cooldown is
60 seconds. That is an admission-invariant question for a separate ADR.
