# Data Model: CLI Setup DX

**Feature**: 339-cli-setup-dx | **Date**: 2026-09-05

---

## 1. Config File Schema (`verdict.yaml`)

The canonical user config lives at `${XDG_CONFIG_HOME:-$HOME/.config}/verdict/verdict.yaml`.

### Fields

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `primary_model` | string | no | `anthropic/claude-3-opus-20240229` | Primary model ID passed to the gate |
| `gateway_url` | string | no | detected or null | OmniRoute/9router base URL (e.g. `http://localhost:20128`) |
| `log_path` | string | no | `verdict-decisions.jsonl` | Decision log output path |
| `providers` | map[string, ProviderConfig] | no | `{}` | Provider-level overrides |
| `offline_mode` | bool | no | `false` | Force offline; skip all gateway probes |

### ProviderConfig (nested)

| Field | Type | Description |
|---|---|---|
| `api_key` | string | Provider API key (warn: never commit) |
| `base_url` | string | Provider base URL override |
| `timeout_ms` | integer | Request timeout in milliseconds |

### Validation Rules

- `gateway_url` must be a valid HTTP/HTTPS URL or null.
- `log_path` must be a writable path or relative path (resolved from cwd).
- `providers` keys must be known provider IDs (validated at read time, not at write time).

---

## 2. Gateway Identity Model

Gateway detection produces a `GatewayCandidate` record:

| Field | Type | Source | Notes |
|---|---|---|---|
| `host` | string | probe target | Always `127.0.0.1` for local probes |
| `port` | integer | probe target | e.g. `20128` |
| `url` | string | constructed | `http://{host}:{port}` |
| `health_ok` | bool | `GET /api/health` → `status == "ok"` | False if TCP fails or HTTP returns non-200 |
| `identity` | string | `"omniroute"` / `"9router"` / `"unknown"` | From `/api/settings` → `runtimePorts` or identity header |
| `display_name` | string | derived | Human label for setup wizard prompt |

### Gateway Port Assignments (corrected from bug FR-014)

| Port | Service |
|---|---|
| 20128 | OmniRoute (primary) |
| 20129 | 9router |
| 20132 | OmniRoute (alternate) |

### Detection Sequence

1. For each probe port in order `[20128, 20129, 20132]`: TCP connect (timeout 500ms)
2. If TCP succeeds: `GET /api/health` (timeout 1s) — must return JSON with `status == "ok"`
3. If health passes: `GET /api/settings` (timeout 1s) — read `runtimePorts.port` to confirm identity
4. Record `GatewayCandidate` with `health_ok=True` and `identity`
5. Return list of all candidates; first healthy candidate is auto-selected by setup wizard

---

## 3. Environment Variable Taxonomy

Variables grouped by purpose (matches `.env.example` grouping):

### Group: Routing

- `OMNIROUTE_BASE_URL` — OmniRoute/9router base URL (no trailing slash)
- `OMNIROUTE_API_KEY` — Auth token for OmniRoute
- `OMNIROUTE_MANAGEMENT_TOKEN` — Management-scope token
- `OMNIROUTE_USAGE_API_KEY_ID` — Usage tracking key ID
- `OMNIROUTE_ALLOW_PRIVATE_HOSTS` — Allow private/LAN hosts (bool string)

### Group: API Server (verdict serve)

- `LLMGATE_HOST` — Bind address (default: `127.0.0.1`)
- `LLMGATE_UNIX_SOCKET` — Unix socket path (alternative to TCP)
- `LLMGATE_AUTH_TOKEN` — Gate auth token
- `LLMGATE_ALLOW_ANONYMOUS` — Allow unauthenticated calls (bool string)
- `LLMGATE_UPSTREAM_BASE_URL` — Upstream routing URL (default: `http://localhost:20128/v1`)
- `LLMGATE_UPSTREAM_API_KEY` — Upstream auth (falls back to `OMNIROUTE_API_KEY`)
- `LLMGATE_UPSTREAM_TIMEOUT_MS` — Upstream request timeout (default: `30000`)
- `LLMGATE_PRIMARY` — Primary model ID
- `LLMGATE_LOG_PATH` — Decision log path
- `LLMGATE_DISCOVERY_TTL_SECONDS` — Discovery cache TTL (default: `60`)
- `LLMGATE_AVAILABILITY_PROFILE` — `development` or `production`
- `LLMGATE_ALLOW_LIVE_PROBES` — Consent to network probes (bool string)
- `LLMGATE_PROBE_BASE_URL` — Override probe target URL
- `LLMGATE_PROBE_API_KEY` — Probe auth token
- `LLMGATE_PROBE_CONSENTED` — Probe consent flag (bool string)
- `LLMGATE_INTELLIGENCE_PROFILE` — `development` or `production`
- `LLMGATE_INTELLIGENCE_TIMEOUT_MS` — Intelligence request timeout
- `LLMGATE_FRONTIER_ALLOWLIST` — Comma-separated allowed frontier model IDs
- `LLMGATE_MODEL_ALLOWLIST` — Comma-separated allowed model IDs
- `LLMGATE_MODEL_DENYLIST` — Comma-separated denied model IDs
- `LLMGATE_MODEL_PASSPORT_TTL` — Model passport TTL seconds (default: `300`)
- `LLMGATE_MAX_REQUEST_BYTES` — Max request body size bytes

### Group: Memory

- `VERDICT_MEMORY_DB` — Memory DB path (default: `~/.verdict/memory.db`)
- `VERDICT_MEMORY_PLANE_PATH` — Memory plane path
- `PI_API_KEY` — Pi AI integration key (optional memory bridge)
- `HERMES_API_KEY` — Hermes integration key (optional memory bridge)

### Group: Toolchain / Runtime

- `XDG_CONFIG_HOME` — Config root (default: `~/.config`)
- `VERDICT_RECEIPTS_DB` / `VERDICT_EVIDENCE_DB` — Evidence receipt store path
- `VERDICT_EVIDENCE_MAX_ENTRIES` — Max evidence entries (default: `256`)
- `VERDICT_RUNTIME_STATE_DIR` — Runtime state directory
- `VERDICT_GUIDANCE_PATH` — Guidance YAML path
- `VERDICT_GUIDANCE_LOCAL_PATH` — Local guidance override path
- `VERDICT_RUFLO_ROOT` — Local Ruflo checkout path
- `VERDICT_RUFLO_REF` — Ruflo git ref
- `VERDICT_RUVECTOR_ROOT` — Local RuVector checkout path
- `VERDICT_RUVECTOR_REF` — RuVector git ref

### Group: Providers (Cloud — optional)

- `OPENAI_API_KEY` — OpenAI API key (for probe transport)
- `GEMINI_API_KEY` — Google Gemini API key
- `OPENROUTER_API_KEY` — OpenRouter API key
