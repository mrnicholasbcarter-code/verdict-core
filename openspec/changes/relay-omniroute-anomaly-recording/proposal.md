# Proposal: record OmniRoute response anomalies on relayed replies

## Why

`verdict serve` relays coding-harness requests to OmniRoute. OmniRoute 3.8.53
can answer from its exact-match cache (`X-OmniRoute-Cache: HIT`) or resend a
request to another model (`X-OmniRoute-Emergency-Fallback`) without failing
it. The `omniroute-evidence-safety` change names these markers fail-closed and
voids probe proof on them. It left one follow-up open: relayed replies do not
record the markers. The receipt only had `cache_hit`, read from
`X-OmniRoute-Cache-Hit`, which misses `X-OmniRoute-Cache: HIT` and the
emergency-fallback marker. An operator or `verdict doctor` therefore cannot
tell from a relay receipt that a reply was replayed or came from another model.

## What Changes

- `verdict.outcome_log.observe_execution` adds `omniroute_anomalies`: the
  names from the shared fail-closed matcher
  (`verdict.http_safety.omniroute_response_anomalies`), keeping repeated
  headers. Every outcome receipt row (`verdict-outcomes.jsonl`) carries it.
- The relay's evidence outcome event (`outcome_event.details`), buffered and
  streamed, carries `omniroute_anomalies` for the attempt whose reply the
  client received.
- Only the fixed names `cache_hit`, `compressed` and `emergency_fallback` are
  stored, never a header value.
- Recording only: the relay still returns the reply. Status, retry,
  dispatch-observation and health-cache behaviour are unchanged.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `omniroute-evidence-safety`: adds relay anomaly recording.

## Impact

- `verdict/outcome_log.py`, `verdict/api.py`, `docs/CLI_REFERENCE.md`.
- Outcome receipts gain one field. Existing fields, including `cache_hit`,
  keep their meaning.

## Evidence boundary

Stub-transport tests only. No live OmniRoute reply has been relayed through
this code yet.

## Not in this change (needs an operator decision)

- Whether a relayed reply with `cache_hit` or `emergency_fallback` should still
  record a healthy dispatch observation (`record_dispatch_observation`), or
  count as accepted in the attempt ledger.
- What a worker reply that carries a cache or fallback marker should do:
  reject it, replace the attempt, or record only. This applies to
  `DirectGatewayExecutor`. `worker_runtime` reads Prime's terminal journal and
  sees no response headers.
