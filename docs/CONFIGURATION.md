# Configuration Reference

> **Shipped behavior (BOD-180 / BOD-182).** Routing configuration is YAML under
> XDG. A separate TOML path exists only for context-hydration settings.
> Environment variables listed below are those the current code actually reads.

## Configuration surfaces

| Surface | Path | Owns |
|---|---|---|
| Routing / setup YAML | `~/.config/verdict/verdict.yaml` (or `$XDG_CONFIG_HOME/verdict/verdict.yaml`) | `primary_model`, `providers` written by `verdict setup` / detect |
| Context hydration TOML | `.verdict/config.toml` or `~/.verdict/config.toml` | Narrow `[context]` settings for pack hydration only |
| Process environment | shell / service env | Overrides for gateway, API, intelligence, receipts, guidance |

There is **no** supported `VERDICT_CONFIG` env var and no full TOML schema for
eligibility/capacity/telemetry/dashboard. Those sections previously documented
here were aspirational and have been removed.

---

## Routing YAML (`verdict.yaml`)

Written by `verdict setup` and provider detection:

```yaml
primary_model: anthropic/claude-opus-5
providers: {}
```

Notes:

- `primary_model` is a preferred identity used when resolving defaults. It is
  **not** a silent fallback when eligibility returns an empty set.
- Live low-criticality `verdict route` admits a concrete **free-tier ∩ active**
  identity and fails closed with a named receipt when the intersection is empty.
  See [`guides/free-tier-admit-smoke.md`](guides/free-tier-admit-smoke.md).

---

## Context hydration TOML (optional)

Used only by context hydration (`verdict/context_hydrate.py`):

```toml
# .verdict/config.toml  (project) or ~/.verdict/config.toml (global)
[context]
# Keys accepted by the current hydration reader only.
# Do not put gateway/routing settings here.
```

---

## Environment variable overrides

### Gateway / OmniRoute

| Variable | Meaning |
|----------|---------|
| `OMNIROUTE_BASE_URL` | Base URL of the local gateway (inventory/execute/health) |
| `OMNIROUTE_API_KEY` | API key/token for the configured gateway |
| `OMNIROUTE_MANAGEMENT_TOKEN` | Management-plane token for provider-node admin endpoints |
| `OMNIROUTE_ALLOW_PRIVATE_HOSTS` | Allow private/internal hosts (SSRF guard; default off) |
| `OMNIROUTE_USAGE_API_KEY_ID` | Usage-reporting API key id |

OmniRoute is **never** Verdict's model-metadata source of truth. Core metadata
comes from models.dev + LiteLLM (`verdict/metadata/`). See [ADR-032](adr/ADR-032-core-model-metadata-store.md).

### Serve / upstream proxy (`verdict serve`)

| Variable | Meaning |
|----------|---------|
| `LLMGATE_PRIMARY` | Preferred upstream identity string |
| `LLMGATE_UPSTREAM_BASE_URL` | Upstream base URL (defaults may derive from OmniRoute) |
| `LLMGATE_UPSTREAM_API_KEY` | Upstream API key |
| `LLMGATE_UPSTREAM_TIMEOUT_MS` | Upstream request timeout |
| `LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS` | Allow private upstream hosts |
| `LLMGATE_MODEL_ALLOWLIST` | Comma-separated allowed model ids |
| `LLMGATE_MODEL_DENYLIST` | Comma-separated denied model ids |
| `LLMGATE_INTELLIGENCE_PROFILE` | Advisory ranking profile (`fast` / `balanced` / `thorough`) |
| `LLMGATE_INTELLIGENCE_TIMEOUT_MS` | Intelligence timeout |
| `LLMGATE_ALLOW_CLIENT_MODEL_OVERRIDE` | Allow client-requested model override |
| `LLMGATE_LOG_PATH` | Decision log path |
| `LLMGATE_DISCOVERY_TTL_SECONDS` | Discovery TTL |
| `LLMGATE_FRONTIER_ALLOWLIST` | Comma-separated frontier allowlist |
| `LLMGATE_AVAILABILITY_TTL_SECONDS` | Availability cache TTL |
| `LLMGATE_AVAILABILITY_STALE_WINDOW_SECONDS` | Availability stale-while-revalidate window |
| `VERDICT_ALLOW_UNVERIFIED_DEV` | Opt-in (`1`/`true`) to admit unverified (unknown/error/timeout) candidates for non-protected work when the intelligence profile is `development`. Default off: unknown-state models are excluded as `runtime_truth_absent` |

### Receipts / evidence

| Variable | Meaning |
|----------|---------|
| `VERDICT_RECEIPTS_DB` | Durable API evidence SQLite path (required for authenticated API mode; `:memory:` selects an explicit non-durable store, as the test suite does) |
| `VERDICT_EVIDENCE_DB` | Legacy alias for the durable evidence path |
| `VERDICT_MEMORY_DB` | MemoryPlane SQLite path (failover/replay demos) |

### Optional platform-neutral guidance

Guidance is an experimental, host-neutral boundary. It is disabled by
default, does not read `AGENTS.md` / `CLAUDE.md`, and cannot bypass gates.

| Variable | Default | Meaning |
|----------|---------|---------|
| `VERDICT_GUIDANCE_ENABLED` | `0` | Opt into guidance initialization |
| `VERDICT_GUIDANCE_PATH` | `GUIDANCE.md` | Repository-relative guidance file |
| `VERDICT_GUIDANCE_LOCAL_PATH` | unset | Optional repository-relative local overlay |
| `VERDICT_GUIDANCE_INIT_TIMEOUT_MS` | `1000` | Initialization timeout |
| `VERDICT_GUIDANCE_MAX_BYTES` | `131072` | Maximum bytes per guidance file |
| `VERDICT_GUIDANCE_MAX_RULES` | `1000` | Maximum parsed rules |

Both configured paths must remain inside the process repository root. Status:
`GET /v1/guidance/status`. Execution contract: versioned
`POST /v1/guidance/execute`. Guidance may return `allow`, `approval_required`,
or `deny`, but always reports `authorization: "unchanged"`.

---

## Example: local gateway

```bash
export OMNIROUTE_BASE_URL=http://localhost:20128
# export OMNIROUTE_API_KEY=...   # only when the gateway requires it

verdict detect --json
verdict models
verdict route "summarize the change" --terse
```

An empty free∩active intersection fails closed. There is no automatic upgrade to
a frontier model when eligibility returns nothing.

---

## Related

- [Getting Started](GETTING_STARTED.md)
- [CLI Reference](CLI_REFERENCE.md)
- [Architecture](architecture.md)
- [`.env.example`](../.env.example) — commented inventory of supported env keys
