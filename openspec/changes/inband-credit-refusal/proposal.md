# inband-credit-refusal: treat an HTTP 200 credit refusal as a payment failure

## Why

Live acceptance and `verdict doctor` went red on 2026-11-18. Six `pol/*` routes stayed
TASK_ELIGIBLE and FREE (`assumed_free_no_paid_evidence`) while every request to them failed.
The provider answered HTTP 200 with `finish_reason: stop`, zero completion tokens, no tool
call, and the text "The account behind this API key doesn't have enough credits. Please top
up ...". Verdict read the status code only:

- the probe transports marked the exchange `ok`;
- `is_not_free_signal` did not match, because the phrase list lacked "enough credits" and "top up";
- the relay classified nothing for a 200, so it wrote no feedback and tried no other candidate;
- the acceptance serve check passed on any non-null content (a false green).

The diagnosis was made on the installed build a4929cec against the live gateway. The captured
bodies are outside the repository; the tests use the same shape with invented text sources.

## What Changes

- `verdict/orchestration/provider_catalog.py`: add `is_inband_billing_refusal`, one shared
  detector. A chat completion body is a refusal only when the usage block reports zero
  completion tokens, the message has no tool call, and the text matches a not-free phrase.
  Add "enough credits" and "top up" to the not-free phrases.
- `verdict/prove_at_rest.py`: the chat/tool transport and the agentic transport return a failed
  exchange with `error_category` `payment_required` for a refusal. The probe result is
  route-scoped, so the provider and its pool are not stopped.
- `verdict/api.py`: the relay treats a buffered 200 refusal as a failed attempt. It records the
  existing route-scoped payment feedback (`capacity_evidence` `billing_402_metered`), tries the
  next candidate inside the existing three-attempt bound, and returns HTTP 402 when none is left.
- `verdict/passive_health.py`, `verdict/omniroute_webhook.py`, `verdict/orchestration/health_cache.py`:
  a webhook completion with zero output tokens is not liveness, and observed traffic no longer
  turns a billing negative healthy.
- `verdict/acceptance.py`: the serve check fails on a refusal body.

## Safety

- Admission before ranking is unchanged: the change only removes routes, it never admits one.
- Unknown is never healthy: a refusal now records an explicit negative instead of a success.
- Attempts stay bounded: the relay uses the existing attempt list and transition edges.
- The failure is route-scoped. Free community routes on the same key keep working, so no
  pool or provider cooldown is written.
- A real answer that quotes the phrase is not a refusal, because it reports completion tokens.
- Tests use stub transports. No provider is called.

## Impact

`verdict/orchestration/provider_catalog.py`, `verdict/prove_at_rest.py`, `verdict/api.py`,
`verdict/passive_health.py`, `verdict/omniroute_webhook.py`, `verdict/orchestration/health_cache.py`,
`verdict/acceptance.py`, `docs/CONFIGURATION.md`, `tests/test_inband_credit_refusal.py`.
`verdict/acceptance_toolcheck.py` and the 95% precision floor are unchanged.

## Limits

- A streamed 200 refusal is not detected: the relay only inspects buffered bodies.
- The OmniRoute health matrix carries no token counts, so a refusal seen only there still looks
  like a 200. It cannot revive a route that has a billing negative, but it can still record
  chat-only liveness for a route that has none. Chat-only liveness is a scheduling hint and is
  never admission evidence.
- The orchestrate launch health check still reads the status code only.
