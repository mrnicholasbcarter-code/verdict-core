# Integration Guide

`llm-gate` is designed to be highly interoperable and integrates seamlessly with popular developer agents, proxies, and editor extensions.

---

## 1. Editor Integrations (Cursor & VSCode)

To utilize `llm-gate` within Cursor or VSCode:
1. Ensure the gateway is running locally (default: `http://localhost:20128/api/v1`).
2. Open Cursor Settings -> Models.
3. Add a Custom Endpoint pointing to `http://localhost:20128/api/v1` (with `/chat/completions` as the chat payload endpoint).
4. Enter any API key placeholder (it will be overridden or routed directly to O‍mniRoute credentials as configured in `~/.codex/config.toml`).

---

## 2. Gateway and Proxies (LiteLLM)

For enterprise workspaces managing multiple developer API tokens, you can route `llm-gate` as an upstream model provider inside LiteLLM:

```yaml
model_list:
  - model_name: llm-gate-sol
    litellm_params:
      model: openai/gpt-5.6-sol
      api_base: http://localhost:20128/api/v1
      api_key: sk-placeholder
```

---

## 3. Node.js & TypeScript Middleware (`llm-gate-node`)

Integrate `llm-gate` directly into Express or NextJS backend routes using the `@nickhq/llm-gate-node` package:

```typescript
import { createRouterMiddleware } from "@nickhq/llm-gate-node";

app.use("/v1/chat/completions", createRouterMiddleware({
  apiBase: "http://localhost:20128/api/v1",
  failClosed: true
}));
```
*Note:* The middleware automatically configures non-buffering SSE headers with connection heartbeats to prevent latency overheads.

---

## 4. CLI Developer Agents (Aider, Claude Code, Codex, Hermes)

Run CLI agents by prepending the environment endpoint overrides:

```bash
# For Aider
OPENAI_API_BASE="http://localhost:20128/api/v1" aider

# For Claude Code
# Configure custom system hooks pointing to path/to/llm-gate binary
```
