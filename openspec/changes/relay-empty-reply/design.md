# Design: relay-empty-reply

## Context

The relay loop in `verdict/api.py` (`_relay_completion`) iterates over admitted
candidates and breaks when it receives `result.status_code < 400`. This condition
treats any HTTP 200 as a success regardless of message content. Reasoning-capable
models (Gemini Flash Lite and others) can consume their entire output budget on
internal reasoning tokens, producing `finish_reason: "length"` with an empty or absent
content field. The relay records `outcome: success` and forwards the empty 200 to the
client. The acceptance check correctly detects the empty content and fails, creating a
persistent mismatch between what the relay considers success and what the client can use.

The existing `inband_billing_refusal` pattern solves an analogous problem: an HTTP 200
that carries no usable content (a credit refusal disguised as a success) is projected
to 402 and the loop retries. The truncated-empty fix mirrors that pattern exactly.

## Goals / Non-Goals

**Goals:**
- Detect buffered HTTP 200 responses where every choice is a length-truncated reply with
  no usable content and retry the next admitted candidate.
- Record the failed attempt with `failure_class: "truncated_empty_reply"` without writing
  a route health negative (prompt-budget event, not a route availability event).
- Handle the n>1 case correctly: if any choice has usable content the body is not
  truncated-empty.
- Raise the serve-check probe's `max_tokens` from 16 to 256 to reduce the probability
  of triggering the condition in the first place.

**Non-Goals:**
- Inspecting streamed responses (only buffered bodies are inspected).
- Fixing the context pack size that causes the original token exhaustion.
- Changing admission, ranking, or the health feedback loop for other failure types.
- Raising the probe budget above the minimum needed for a one-word reply.

## Decisions

1. **Mirror `inband_refusal` exactly.** Project the matching 200 to 422 (outside the
   feedback-write gate `== 402 or >= 500`), extend `previous_refused`, and continue the
   retry loop. 422 is chosen because it is >= 400 (treated as failure everywhere), not
   402 (which would trigger the payment feedback path), and not >= 500 (which would
   trigger the upstream-transport feedback path).

2. **All-choices check.** With `n > 1`, choice 0 could be truncated-empty while
   another choice has content. `is_truncated_empty_reply` iterates every choice and
   returns True only when all of them match the truncated-empty condition. A non-Mapping
   entry, a non-empty content field, or a tool-call response in any choice causes
   immediate False.

3. **No health negative.** `truncated_empty_reply` is intentionally not added to
   `_RELAY_FEEDBACK_CATEGORIES`. The model produced a valid HTTP 200; the prompt budget
   was exhausted. This is not evidence of route unavailability.

4. **Named constant `_SERVE_PROBE_MAX_TOKENS = 256`.** Placing it near the other
   module-level constants in `verdict/acceptance.py` makes the value visible and
   searchable. 256 gives enough headroom for a one-word reply after typical reasoning
   overhead (10–15 tokens on Gemini Flash Lite).

## Risks / Trade-offs

- **False-positive suppression.** A model that always returns empty content on this
  route would cause the relay to exhaust all candidates and return a named error rather
  than the empty 200. This is strictly better: the client receives an actionable error
  code instead of an empty reply recorded as success.
- **Partial answers.** `finish_reason: "length"` with non-empty content is a partial
  answer and passes through unchanged. The detector checks content, not finish_reason
  alone.
- **Streamed responses.** Streaming paths are not affected. A reasoning model that
  exhausts its streaming budget similarly returns a truncated stream; that is a separate
  concern.
- **Higher probe budget.** 256 tokens is 16x the original. The probe sends one
  short message ("Reply with the single word OK.") with no context pack; the extra
  tokens have negligible cost on a free-tier route.

## Security / trust implications

No change to trust boundaries. The fix operates on the relay's internal representation
of an upstream response after it has been received and buffered. The 422 projection is
internal; the client receives either the synthetic error body or a successful reply from
a later candidate. No new external calls are made.

## Routing / context / memory implications

The fix only affects the relay loop for `POST /v1/chat/completions` and
`POST /v1/responses` on buffered (non-streaming) responses. Routing, admission, ranking,
and the health cache are unchanged. `is_truncated_empty_reply` is a pure function with
no side effects; it reads the response body only.

## Migration / rollback

Rollback: revert the `truncated_empty` block in `api.py`, the `is_truncated_empty_reply`
function in `provider_catalog.py`, and the `_SERVE_PROBE_MAX_TOKENS` constant in
`acceptance.py`. After rollback the relay will again record empty 200s as successes and
the acceptance check will continue to fail on reasoning models with tight token budgets.

No schema or database changes. No service restart beyond a normal reinstall.

## ADR impact

No new ADR required. This is a bug fix to the relay loop and the acceptance probe. The
pattern mirrors the existing `inband_billing_refusal` decision (already in production);
no new architectural principle is introduced.
