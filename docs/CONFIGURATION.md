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
| `gateway_start_command` | config file `gateway_start_command` | env `VERDICT_GATEWAY_START_COMMAND` | — (no default; never guessed) |
| `gateway_ready_timeout_s` | config file `gateway_ready_timeout_s` | env `VERDICT_GATEWAY_READY_TIMEOUT_S` | `30.0` |
| `primary_model` | config file `primary_model` | env `LLMGATE_PRIMARY` | `verdict.contracts.DEFAULT_PRIMARY_MODEL` |
| `profile` | config file `profile` | env `LLMGATE_INTELLIGENCE_PROFILE` | `development` |
| `log_path` | config file `log_path` | env `LLMGATE_LOG_PATH` | `verdict-decisions.jsonl` |
| any env name | exported process environment | credential store (`credentials.env`) | — |

The credential store is **not** a source for `gateway_start_command` or
`gateway_ready_timeout_s`. A command is not a secret, and reading an argv out of
a secrets file would turn that file into a command-injection surface. Both are
config-file-first, then the exported environment only.

`providers` is config-first while `gateway_url` is environment-first. This is
deliberate: both match shipped behaviour. When the config file and
`OMNIROUTE_BASE_URL` disagree about the gateway provider, the config file wins
for the provider map and the resolver emits a non-fatal `precedence_conflict`
diagnostic naming both values.

`BootstrapResult.field_sources` records which source won for every field, so a
value is never applied without a traceable origin.

#### `verdict serve` precedence

`verdict serve` resolves three fields on its own, because a long-running server
must not change identity, profile or decision log the moment a routing config
appears on disk:

| Field (serve path only) | 1st | 2nd | 3rd |
|---|---|---|---|
| `primary_model` | env `LLMGATE_PRIMARY` | `verdict.contracts.DEFAULT_PRIMARY_MODEL` | — (routing config not read) |
| `profile` | env `LLMGATE_INTELLIGENCE_PROFILE` | `development` | — (routing config not read) |
| `log_path` | env `LLMGATE_LOG_PATH` | `verdict-decisions.jsonl` | — (routing config not read) |

The routing config is still read at serve startup, only to detect disagreement.
When `verdict.yaml` asks for a different value than the serve path applies, a
non-fatal `precedence_conflict` diagnostic is written to stderr naming both
sources, both values, and the environment variable that would apply the config
value. A `profile: production` in `verdict.yaml` therefore does not flip the
serve profile; export `LLMGATE_INTELLIGENCE_PROFILE=production` for that.

Every other field — `providers`, `gateway_url`, credential names — follows the
table above on the serve path too.

### Gateway decision

`BootstrapResult` exposes the gateway decision as data, never as an action:

- `gateway_required: bool` — true when the resolved provider set contains an
  OmniRoute-shaped binding (named `omniroute`, or declaring
  `api_key_env: OMNIROUTE_API_KEY`).
- `gateway_url: str | None` — the resolved gateway URL.

- `gateway_start_command: tuple[str, ...] | None` — explicit argv that starts a
  local gateway, or `None` when the operator configured none.
- `gateway_ready_timeout_s: float` — readiness budget for the lifecycle owner's
  bounded wait (default `30.0`).

Gateway health is checked only through `verify_gateway_reachable(result,
probe=...)` with a caller-supplied probe. Nothing in this module opens a socket,
and nothing starts or stops a gateway.

### Gateway lifecycle

`verdict/gateway_lifecycle.py` owns the action side of the gateway decision. It
reads no configuration of its own: whether a gateway is required, at which URL,
with which start command and readiness budget all come from `BootstrapResult`.

| Entry point | Behaviour |
|---|---|
| `inspect_gateway(result, probe=...)` | Report only. Probes at most once, never launches, signals or locks. |
| `ensure_gateway_ready(result, probe=..., launcher=..., clock=..., sleep=..., state_dir=..., observer=...)` | Reuses a healthy gateway, otherwise starts one. Returns the state. |
| `require_gateway_ready(...)` | Same, but raises `BootstrapError("gateway_lifecycle_failed", ...)` when the gateway cannot be made ready. Paths that need the gateway never switch silently. |

States:

| State | Meaning | Ready |
|---|---|---|
| `not_required` | no gateway provider resolved; no probe is performed | yes |
| `already_ready` | an existing gateway answered and is reused, never restarted | yes |
| `started` | this call (or the lock owner it waited for) made the gateway ready | yes |
| `starting` | transient; reported to `observer` while launching or waiting | — |
| `failed_to_start` | no start command, a remote URL, a launch error, an unusable lock, an exited process, or a readiness timeout | no |
| `unhealthy` | something answers the URL but is not healthy, or it refused our credential | no |

Guarantees:

- **No duplicates.** A per-gateway `flock` lock lives under the Verdict state
  directory (`$VERDICT_HOME`, else `~/.verdict`), in `gateway/`. Only the lock
  holder may launch; a concurrent caller waits for readiness on the same
  deadline. Two concurrent processes therefore produce exactly one launch.
  The lock identity is the listening socket, not the spelling of the URL:
  `localhost`, `127.0.0.1` and `[::1]` on the same port share one lock, as do a
  trailing slash and a trailing `/v1`.
- **Safe lock handling.** The lock is opened with `O_NOFOLLOW` and permissioned
  through the descriptor, so a symlinked lock path is refused rather than created
  through or re-permissioned. A symlinked state directory is refused too, and an
  unwritable state directory is reported as a `gateway_start_failed`
  `gateway_health` diagnostic rather than raising `PermissionError`.
- **No guessing.** An absent `gateway_start_command` is a
  `gateway_start_command_missing` diagnostic naming the field and its
  environment override. Verdict never discovers a binary and never port-scans to
  invent one.
- **Local only.** A process is started only when `gateway_url` is loopback
  (`127.0.0.1`, `::1`, `localhost`). A non-loopback URL reports
  `gateway_remote_not_managed` and is never launched.
- **No shell.** The command is always an argv list launched with `shell=False`.
  A YAML sequence is taken element-wise; the environment form is `shlex`-split.
- **Bounded.** Readiness uses a deadline plus capped backoff, and the contender
  wait is bounded by the same deadline. `gateway_ready_timeout_s` must be finite,
  positive and no greater than `600`; `inf` and a larger value are configuration
  refusals, because a budget that cannot bound the wait defeats the field.
- **No orphans.** A launched gateway is started in a new session, so it leads its
  own process group, and that group id is recorded at launch. On timeout the group
  is sent `SIGTERM`, waited for, then `SIGKILL`, then reaped, so neither a zombie
  nor a descendant of the gateway survives. This matters for a wrapper such as
  `npx`, `node` or `sh`, where the gateway is a grandchild. Only a group recorded
  at launch is ever signalled, so a pre-existing gateway — and the operator's own
  shell job — can never be reached. The diagnostic states what happened: reaped,
  killed, or still running with its pid.
- **No secrets in output.** A start command routinely carries a token, so the argv
  is never serialized or echoed. `BootstrapResult.to_dict()` renders the program
  name plus a redacted-argument count, and a launch failure has every argument
  scrubbed out of its detail.
- **Authenticated readiness.** The probe sends the gateway API key when one is
  configured, read by the name the provider binding declares (`api_key_env`), from
  the exported environment and then the credential store. A `401` or `403` is
  reported as `gateway_auth_failed`, distinct from `gateway_unhealthy`, so an
  auth-protected gateway is not called broken. The key is never printed.
- **Inherited child environment.** A launched gateway inherits the operator's
  environment as it stands (`os.environ`). Verdict adds nothing to it, and in
  particular never copies credential-store values into it — store values are
  passed to the resolver as an argument and are never exported. Any variable the
  gateway needs must therefore be exported by the operator.

Surfaces:

- `verdict doctor` prints a report-only `Gateway lifecycle` section and includes
  a `gateway_lifecycle` block in `--json`. It never starts anything, and it does
  not change the exit code.
- `verdict serve` records the state in its startup report. With both opt-ins off
  it performs no gateway I/O at all: the state is `not_probed` and no startup line
  is printed, so booting the server never depends on whether a gateway happens to
  be listening. Startup stays non-fatal.
  `VERDICT_SERVE_ENSURE_GATEWAY=true` opts into the active path, and
  `server_bootstrap_diagnostics(gateway_probe=...)` opts into a report-only probe
  with a caller-supplied transport.
- CLI execution through a configured gateway calls `require_gateway_ready` when
  `VERDICT_ENSURE_GATEWAY=true`; a failed ensure exits `1` with the
  `gateway_health` diagnostic rather than executing elsewhere.

Both opt-ins default to off, so no command starts a process the operator did not
ask for.

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
| `configuration` | an input is missing, malformed or only a default | `config_file_missing`, `config_file_unreadable`, `config_file_unparsable`, `config_file_not_mapping`, `config_providers_malformed`, `provider_entry_malformed`, `provider_base_url_missing`, `provider_base_url_invalid`, `no_provider_configuration`, `default_providers_forbidden`, `default_provider_fallback`, `default_primary_model`, `credential_env_missing`, `precedence_conflict`, `gateway_start_command_malformed`, `gateway_ready_timeout_invalid` |
| `gateway_health` | configuration is complete, the gateway is not answering or could not be started | `gateway_required_but_absent`, `gateway_unreachable`, `gateway_start_command_missing`, `gateway_start_failed`, `gateway_process_exited`, `gateway_ready_timeout`, `gateway_unhealthy`, `gateway_auth_failed`, `gateway_remote_not_managed` |
| `model_eligibility` | configuration and gateway are fine, no model qualifies | owned by the eligibility gate, not by bootstrap |

`config_file_missing`, `default_provider_fallback`, `default_primary_model`,
`credential_env_missing` and `precedence_conflict` are non-fatal notes
(`fatal=False`), available via `BootstrapResult.notes()`. Every other code is a
refusal.

`default_primary_model` fires whenever `primary_model` falls through to
`verdict.contracts.DEFAULT_PRIMARY_MODEL`, including under
`require_authoritative=True`. `IntelligenceService` returns `primary_model` as
its tier-0 / no-offload-match decision, so a defaulted identity can execute work;
the note keeps that visible. It is a note, not a refusal: whether authoritative
bootstrap should refuse a defaulted identity outright is an open decision.

`describe_bootstrap_failure(exc)` renders a deterministic operator report. The
Prime supervisor still raises `ControllerLaunchError("production_factory_unavailable", ...)`
for wire compatibility, but the detail now carries the code, class, field, source
and remediation rather than an opaque message.

### Credential store

Any environment name the contract reads (`OMNIROUTE_BASE_URL`,
`OMNIROUTE_API_KEY`, `LLMGATE_PRIMARY`, ...) may come from the local credential
store (`$XDG_CONFIG_HOME/verdict/credentials.env`, 0600) instead of the exported
environment. The exported environment always wins; a name supplied only by the
store reports `source="credential_store"` and does not raise
`credential_env_missing`.

`load_credential_store_env()` is the seam. The Prime supervisor and both API
bootstrap paths (`verdict serve` startup and `server_bootstrap_diagnostics`) pass
its result to `resolve_provider_bootstrap(credential_store_env=...)`. Store
values are passed as an argument and are never exported into `os.environ`, so a
stored credential does not leak into child processes. A missing, unreadable or
insecurely permissioned store is not a bootstrap failure: it resolves to `{}` and
any name that stays unset is still reported as `credential_env_missing`.

### Secrets in rendered output

Diagnostics are written to stderr, receipts and structured logs, so they never
echo secret material:

- API keys are referenced by environment-variable name (`api_key_env`), never by
  value. This holds for exported environment values and credential-store values
  alike.
- A URL that carries `user:password@` userinfo is rendered as `***:***@host`
  everywhere: diagnostic details, `ProviderBinding.to_dict`,
  `BootstrapResult.to_dict`, `describe_bootstrap_failure` and the CLI/library
  stderr notes. The runtime binding keeps the operator's real URL, so execution
  is unaffected.
- `config_file_unparsable` reports the parser's problem plus the 1-based line and
  column. It never embeds the offending source line, which could itself hold a
  credential.

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
| `VERDICT_GATEWAY_START_COMMAND` | Explicit command that starts a local gateway (shlex-split into an argv list; never run through a shell; the launched gateway inherits the exported environment) |
| `VERDICT_GATEWAY_READY_TIMEOUT_S` | Readiness budget in seconds for the bounded gateway wait (default `30`; must be finite, positive and at most `600`) |
| `VERDICT_ENSURE_GATEWAY` | Opt in to ensuring the gateway is ready before CLI execution (default off) |
| `VERDICT_SERVE_ENSURE_GATEWAY` | Opt in to ensuring the gateway is ready at `verdict serve` startup (default off) |

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
