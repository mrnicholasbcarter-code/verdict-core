# Configuration Reference

> **Shipped behavior.** Routing configuration is YAML under
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

## Provider bootstrap contract

`verdict/provider_bootstrap.py` is the single supported way to construct a
provider/gateway configuration. The CLI (`verdict route`, `verdict compare`),
the API serve path, the Prime supervisor and the test suite all resolve through
`resolve_provider_bootstrap()`, so they share one precedence and one set of
diagnostics.

The resolver is offline. Reading the routing YAML is its only I/O: no network
call, no port scan, no gateway start or stop. It returns a `BootstrapResult`, or
raises `BootstrapError` naming the exact missing field, its source and the
remediation.

### Precedence

Highest source first. Only the sources listed below are consulted.

| Field | 1st | 2nd | 3rd |
|---|---|---|---|
| `providers` | config file `providers` | env `OMNIROUTE_BASE_URL` | built-in local set (opt-in only, see below) |
| `gateway_url` | env `OMNIROUTE_BASE_URL` | config file `gateway_url` | resolved gateway provider `base_url` |
| `primary_model` | config file `primary_model` | env `LLMGATE_PRIMARY` | `verdict.contracts.DEFAULT_PRIMARY_MODEL` |
| `profile` | config file `profile` | env `LLMGATE_INTELLIGENCE_PROFILE` | `development` |
| `log_path` | config file `log_path` | env `LLMGATE_LOG_PATH` | `verdict-decisions.jsonl` |
| any env name | exported process environment | credential store (`credentials.env`) | — |

`providers` is config-first while `gateway_url` is environment-first. This is
deliberate: both match shipped behaviour. When the config file and
`OMNIROUTE_BASE_URL` disagree about the gateway provider, the config file wins
for the provider map and the resolver emits a non-fatal `precedence_conflict`
diagnostic naming both values.

`BootstrapResult.field_sources` records which source won for every field, so a
value is never applied without a traceable origin.

### Gateway decision

`BootstrapResult` exposes the gateway decision as data, never as an action:

- `gateway_required: bool` — true when the resolved provider set contains an
  OmniRoute-shaped binding (named `omniroute`, or declaring
  `api_key_env: OMNIROUTE_API_KEY`).
- `gateway_url: str | None` — the resolved gateway URL.

Gateway health is checked only through `verify_gateway_reachable(result,
probe=...)` with a caller-supplied probe. Nothing in this module opens a socket,
and nothing starts or stops a gateway.

### Built-in local providers

There is no silent fallback to an unintended provider. A provider set must come
from a canonical source, with one narrow exception for interactive local use:

- A caller may pass `allow_default_providers=True` to accept the built-in local
  set (`DEFAULT_LOCAL_PROVIDERS`). That path is never silent —
  `field_sources["providers"]` becomes `"default"` and a
  `default_provider_fallback` diagnostic names the default and how to configure
  it explicitly. The CLI prints it to stderr.
- When `profile` is `production`, or the caller passes
  `require_authoritative=True`, a defaulted provider set is a `configuration`
  `BootstrapError` and execution does not proceed. The Prime supervisor always
  requires authoritative bootstrap.

### Diagnostic classes

Every diagnostic carries a class, so startup output distinguishes the three
failure kinds instead of collapsing them into one message.

| Class | Meaning | Codes |
|---|---|---|
| `configuration` | an input is missing, malformed or only a default | `config_file_missing`, `config_file_unreadable`, `config_file_unparsable`, `config_file_not_mapping`, `config_providers_malformed`, `provider_entry_malformed`, `provider_base_url_missing`, `provider_base_url_invalid`, `no_provider_configuration`, `default_providers_forbidden`, `default_provider_fallback`, `credential_env_missing`, `precedence_conflict` |
| `gateway_health` | configuration is complete, the gateway is not answering | `gateway_required_but_absent`, `gateway_unreachable` |
| `model_eligibility` | configuration and gateway are fine, no model qualifies | owned by the eligibility gate, not by bootstrap |

`config_file_missing`, `default_provider_fallback`, `credential_env_missing` and
`precedence_conflict` are non-fatal notes (`fatal=False`), available via
`BootstrapResult.notes()`. Every other code is a refusal.

`describe_bootstrap_failure(exc)` renders a deterministic operator report. The
Prime supervisor still raises `ControllerLaunchError("production_factory_unavailable", ...)`
for wire compatibility, but the detail now carries the code, class, field, source
and remediation rather than an opaque message.

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
| `LLMGATE_INTELLIGENCE_PROFILE` | Intelligence profile. Default `development` (`verdict.intelligence.DEFAULT_PROFILE`). `production` makes every `IntelligenceService.route` caller (CLI `route`, supervisor) require an execution-path authority `ExecutionPathDecision` (`serve_path_authority_required`), makes `VERDICT_ALLOW_UNVERIFIED_DEV` inert, and makes `/v1/route/explain` exclude unverified candidates. The default is kept `development` on purpose: fail-closed admission does not depend on it (unknown/error/timeout candidates are excluded unless `VERDICT_ALLOW_UNVERIFIED_DEV=1`), and the API serve path forces execution-path authority in every profile |
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
export OMNIROUTE_BASE_URL=http://127.0.0.1:20128   # plain-HTTP gateways must use a loopback IP literal, not a hostname
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
