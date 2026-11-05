# Proposal: BOD-219 OmniRoute evidence-safety headers

## Why

Verdict treats OmniRoute replies as evidence: probe passes admit routes, tool
probes prove tool calling, and worker replies become receipts. OmniRoute 3.8.53
can change or fake those replies without failing the request:

- An exact-match response cache answers `temperature: 0` requests, tool-call
  replies included, for 30 minutes without an upstream call. The only marker is
  `X-OmniRoute-Cache: HIT`. Verdict's liveness and tool probes use
  `temperature: 0`.
- Context compression can rewrite messages and tool results. Even the lossless
  `lite` mode strips trailing whitespace, which corrupts diffs. Marker:
  `X-OmniRoute-Compression: <mode>; source=...`.
- Memory and skills injection rewrites messages and tools when enabled on the
  server.
- `X-OmniRoute-Emergency-Fallback` marks a reply from a different model.

OmniRoute honours per-request opt-outs: `x-omniroute-no-cache: true`,
`x-omniroute-compression: off`, `x-omniroute-no-memory: true`. Before this
change only the patch executor and autodev sent the no-cache header.

## What Changes

- One shared helper in `verdict/http_safety.py`:
  `omniroute_evidence_headers()` returns the three opt-outs plus a fresh
  `X-Request-Id`; `omniroute_response_anomalies()` names `cache_hit`,
  `compressed` and `emergency_fallback` from response headers, failing closed:
  a marker is safe only when absent or carrying the exact value OmniRoute
  3.8.53 sends when nothing happened (`MISS`, `false`, mode `off`, `false`);
  `untrusted_response_reason()` maps a cache hit or emergency fallback to a
  named reason (`omniroute_cache_hit`, `omniroute_emergency_fallback`).
- The headers go on every Verdict-owned inference request in the probe and
  worker transports: `openai_probe_transport` (`verdict probe`, lazy API
  probes, decomposer, intelligence, patch executor, autodev), prove-at-rest
  chat/tool and agentic transports (daemon, verified refresh, census, identify
  screen), eligibility health probes (`openai_health_probe`), the acceptance
  tool check, the census needle probe, `DirectGatewayExecutor`, and the relay
  upstream call in `verdict/proxy.py` (chat and responses, not `GET /models`).
- A probe reply marked as a cache hit or emergency fallback is not proof:
  - `ProbeRunner` records it as a failed, non-ready observation with the named
    error class, without counting it toward quarantine.
  - prove-at-rest chat, tool and liveness probes return a no-write outcome with
    the named reason, so no health is written and prior proof ages out.
  - agentic probes write no capability entry; the identify screen reports the
    named reason. Both decide before any cache write.
  - the acceptance tool check fails the route with the named reason.
  - the eligibility health probe is unhealthy with the named category.
  - the census needle probe fails.
- `compressed` is reported but does not void proof on its own.

## Capabilities

### New Capabilities

- `omniroute-evidence-safety`: Verdict asks OmniRoute not to cache, compress or
  inject into its inference requests, and never counts a cached or
  emergency-fallback probe reply as proof.

### Modified Capabilities

## Impact

- `verdict/http_safety.py` (shared helper), `verdict/probes.py`,
  `verdict/prove_at_rest.py`, `verdict/subagent_selection.py`,
  `verdict/acceptance_toolcheck.py`, `verdict/omniroute_pipeline/census_hook.py`,
  `verdict/orchestration/executors.py`, `verdict/proxy.py`.
- New tests in `tests/test_bod219_omniroute_safety_headers.py`.
- No new dependency, setting or service. Relay traffic now also opts out of
  OmniRoute caching and compression.

## Evidence boundary

KNOWN: the header names, opt-out values and cache, compression and memory
behaviour come from the OmniRoute 3.8.53 source reading recorded in BOD-219
(2026-11-05). KNOWN: the safe marker values come from the installed
OmniRoute 3.8.53 package source (file and line cited in
`verdict/http_safety.py`). KNOWN: the tests use stub transports only. NOT AVAILABLE: a live
run against OmniRoute that shows the opt-outs are honoured and no `HIT` marker
is returned. That needs a live check on the operator's gateway.

## Acceptance criteria

- Every touched transport sends `X-OmniRoute-No-Cache: true`,
  `X-OmniRoute-Compression: off`, `X-OmniRoute-No-Memory: true` and a unique
  `X-Request-Id` on each inference request.
- A probe reply with `X-OmniRoute-Cache: HIT` or
  `X-OmniRoute-Emergency-Fallback` is never counted as healthy, liveness, tool
  or agentic proof, and carries a named reason.

Open follow-ups, not part of this change: the relay response recording in
`verdict/api.py` (anomaly recording on relayed replies); `savings_live`,
`free_tier_admit.execute_offload_chat`, `live_routing`, `routing_demo` and
`workers.py` request paths; anomaly handling for worker replies in
`DirectGatewayExecutor`.
