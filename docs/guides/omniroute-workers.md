# OmniRoute workers

Verdict workers use the local OmniRoute gateway as the single provider boundary.
They do not contain an OpenRouter key or a provider allowlist.

## Provider and MCP discovery

OmniRoute is the runtime gateway, not merely an OpenAI-compatible URL. Before
selecting a worker model, treat the gateway as a capability-discovered
boundary:

1. Read the public model catalog from `GET /v1/models` (or the authenticated
   management catalog when the deployment exposes it).
2. Treat catalog membership as identity/configuration evidence only. It is not
   proof that a provider is healthy, reachable, within quota, or eligible for a
   particular request.
3. If management access is configured, discover optional provider/runtime
   signals from the documented endpoints and use only the capabilities that
   are actually advertised:

   - `GET /api/models/catalog` — provider-grouped catalog, when enabled.
   - `GET /api/mcp/status` and `GET /api/mcp/tools` — MCP service status and
     tool discovery.
   - `GET /api/free-tier/summary` and
     `GET /api/quota/pools/{pool_id}/usage` — quota signals, when enabled.
   - the MCP stream/SSE transport (`/api/mcp/stream` or `/api/mcp/sse`) —
     optional tool access, when enabled by the deployment.

Management and MCP endpoints normally require the configured OmniRoute bearer
token. A `401` or an unavailable optional endpoint is an **unknown** signal;
it must not be converted into “healthy”. Do not read OmniRoute's private
database or copy provider credentials into Verdict. Capability discovery is
optional and fail-closed for protected work.

The OpenAI-compatible `/v1/models` endpoint is intentionally useful without
management credentials in local development, but it still only supplies a
catalog. Verdict's availability adapter/cache is responsible for normalizing
health, quota/headroom, freshness, circuit/cooldown, and capability evidence
before ranking. See [ROUTING_POLICY](../specs/ROUTING_POLICY.md) for the
eligibility invariant.

```python
from verdict import OmniRouteWorkerClient, WorkerPool, WorkerRequest

client = OmniRouteWorkerClient(
    "http://127.0.0.1:20128/v1",
    max_attempts=3,
)
pool = WorkerPool(client, max_concurrency=4)
results = await pool.run([
    WorkerRequest(
        task_id="explore",
        model="auto/best-free",
        messages=[{"role": "user", "content": "Inspect the target module."}],
    ),
])
```

The client fetches `/v1/models` and chooses only advertised IDs. `auto/*`
requests prefer OmniRoute's virtual routes (`auto/best-coding`,
`auto/best-reasoning`, `auto/best-free`, and `auto/fast`) and then fall back to
advertised `:free` models. An explicit provider-prefixed model must be present
in the live catalog. This keeps provider rotation, quota state, breakers, and
model health inside OmniRoute.

The same rule applies to every lower-tier autonomous worker: use OmniRoute's
live catalog and documented discovery APIs, never a hard-coded provider
allowlist. When the gateway cannot provide fresh runtime truth, retain the
candidate as `unknown` for explanation and exclude it from protected work.

Only transient failures (timeouts, connection failures, 408/409/425/429, and
5xx responses) trigger bounded failover. Authentication and malformed-request
responses fail immediately. `WorkerPool` preserves input order and returns a
redacted error per failed task instead of cancelling unrelated workers.

## Codex configuration

The local Codex provider is configured as:

```toml
model = "cx/gpt-5.6-luna-xhigh"
model_provider = "omniroute"

[model_providers.omniroute]
provider = "openai"
base_url = "http://127.0.0.1:20128/v1"
wire_api = "responses"
```

`cx/...` is OmniRoute's canonical Codex namespace. `codex/...` remains a
compatibility alias, but `cx/...` is the preferred spelling for this gateway.
Worker model IDs can use `auto/*`, or any provider-prefixed ID returned by the
live catalog (for example `openrouter/...:free` or `kilocode/...`). They inherit
the parent OmniRoute provider; a worker does not need a second provider block.

The custom Codex agents under `~/.codex/agents/` intentionally use different
OmniRoute model IDs for exploration, implementation, review, and fast bounded
tasks. Keep those IDs catalog-backed and re-run `codex doctor --strict-config`
after changing them.
