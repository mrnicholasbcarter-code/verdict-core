# Enterprise Gateways & Proxies (Portkey, OmniRoute, Peezy)

`llm-gate` can be used in two modes:

1. **Explain-only routing:** call `POST /v1/route` to obtain a model decision, then let another gateway execute the request.
2. **Development proxy:** point an OpenAI-compatible client at `llm-gate` and use `/v1/chat/completions`. The proxy rewrites only the selected model, keeps the remaining request fields, forwards server-owned upstream auth, and passes streamed bytes through unchanged.

The second mode is an implemented alpha slice, not a production-readiness claim. Local auth, live health/headroom, legal retry/fallback, required managed intelligence, and a configured OmniRoute smoke test remain release gates.

## OmniRoute-compatible upstream

Configure the upstream explicitly. The default is the local OmniRoute-compatible address `http://127.0.0.1:20132/v1`.

```bash
export LLMGATE_UPSTREAM_BASE_URL=http://127.0.0.1:20132/v1
export OMNIROUTE_API_KEY=your-local-key
export LLMGATE_PRIMARY=cx/gpt-5.4-mini
llm-gate serve --host 127.0.0.1 --port 8000
```

The upstream URL and credential are process configuration. They are never taken from arbitrary client request fields. The client should target:

```text
http://127.0.0.1:8000/v1
```

Useful checks:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/v1/models
```

`/v1/models` applies the optional exact-match local filters `LLMGATE_MODEL_ALLOWLIST` and `LLMGATE_MODEL_DENYLIST`. Rows include `llm_gate.availability_state: "unknown"` unless a future bounded health/headroom adapter establishes stronger evidence. A catalog row is not proof that a provider is live, healthy, or quota-available.

## Portkey AI

[Portkey](https://portkey.ai) can remain the execution and observability gateway. Use `POST /v1/route` as a policy decision hook when you need a model choice without having `llm-gate` forward request bytes.

## Peezy Gateway

For Peezy or another OpenAI-compatible gateway, use the same explain-only hook pattern or configure it to send compatible chat-completion traffic to the development proxy endpoint. Verify the exact upstream contract with a mock server before using production credentials.

## Scope and verification

The normative requirements are in:

- [`docs/specs/PRODUCT_SPEC_V0.2.md`](../specs/PRODUCT_SPEC_V0.2.md)
- [`docs/specs/RELEASE_ACCEPTANCE.md`](../specs/RELEASE_ACCEPTANCE.md)

The current proxy contract tests cover unknown request fields, tools, server-owned auth, non-streaming response passthrough, arbitrary SSE boundaries, malformed/oversized bodies, catalog filtering, readiness, and stale content-length handling. They do not constitute live OmniRoute validation.
