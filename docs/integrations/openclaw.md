# OpenClaw & NemoClaw Integration

For autonomous agent loops like **OpenClaw** and **NemoClaw**, routing is critical. Because these frameworks loop repeatedly without human intervention, a single runaway process on an expensive model can drain hundreds of dollars.

`llm-gate` acts as a fail-safe firewall for these agents.

### Integration via OmniRoute / Proxy

Because OpenClaw and NemoClaw operate via continuous API HTTP requests, the recommended integration is to put `llm-gate` behind an API Gateway like [OmniRoute](https://github.com/diegosouzapw/OmniRoute).

1. OmniRoute intercepts the OpenClaw / NemoClaw `/v1/chat/completions` request.
2. OmniRoute triggers a fast-webhook to your `llm-gate serve --port 8000` instance.
3. `llm-gate` reads the `messages[-1].content` in the payload.
4. If `llm-gate` detects safe/trivial polling, it returns a T3 model (e.g., `llama-3.1-8b`). OmniRoute proxies the traffic there.
5. If OpenClaw attempts a destructive action or secure refactor, `llm-gate` forces a T0 model (e.g., `opus-4.8`).

This creates a bulletproof autonomous execution boundary.
