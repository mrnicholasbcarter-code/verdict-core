# Configuration Reference

> **Shipped behavior.** Routing configuration is YAML under
> XDG. A separate TOML path exists only for context-hydration settings.
> Environment variables listed below are those the current code actually reads.

## First run

On a new install, before anything is set up:

1. `verdict setup` writes the configuration. Until then `verdict doctor` starts
   with "Verdict is installed but not set up yet. Start with: verdict setup" and
   marks parts that do not exist yet as `NOT-CONFIGURED`, each with its next
   command. These rows are not `OK`, and doctor keeps exiting 1 while they remain.
   A part that exists but is broken is still `FAILED`.
2. `verdict orchestrate` needs a free route with a recorded tool-use (agentic)
   check. A new state directory has none, so the run ends BLOCKED with the reason
   `no_agentic_probe` and prints the command that records the check. Run it once
   (it makes a small, bounded number of model calls and needs your consent flag):

   ```bash
   verdict identify --screen --allow-live-probe --capacity free --max-per-provider 1 --max-routes 8
   ```

   Then run `verdict orchestrate` again. `verdict identify install-service`
   installs the timers that keep this evidence fresh. Admission rules do not
   change: routes without a fresh check are never used.
3. `verdict serve` needs `LLMGATE_AUTH_TOKEN` and `VERDICT_RECEIPTS_DB`; see
   [Serve authentication and receipts](#serve-authentication-and-receipts).

## Worker executor and optional harnesses

`verdict orchestrate` runs workers with the `direct-gateway` executor by
default: each worker request goes straight to the gateway, so no agent harness
has to be installed. Prime Agent is an optional harness. Select it with
`--executor prime-headless` (alias `prime`), or per node with
`--executor-map node-1=prime-headless`.

- Route visibility follows the executor. Plain `verdict eligibility` and the
  default run read the live gateway catalog; Prime's model registry
  (`models.json`) is read only when Prime is selected or with
  `verdict eligibility --sync-visibility`.
- On a host without the Prime Agent binary, `verdict setup` plans no Prime
  registry action, `verdict doctor` shows the Prime rows as `not-installed`
  (optional, not an issue), and the hourly live acceptance job reports the
  `harness-prime` check as skipped with the reason
  `Prime Agent not installed (optional harness)`. A skipped check is never
  counted as passed.
- On a host with the Prime binary whose Verdict harness is not enabled, the
  check is skipped with the reason
  `Prime Agent installed but not enabled (optional harness)`, and
  `verdict doctor` shows a `Prime harness` WARNING row (not an issue) with
  `repair: verdict harness prime enable`. When Prime is installed and enabled, all three behave as
  before and can fail.

## Configuration surfaces

| Surface | Path | Owns |
|---|---|---|
| Routing / setup YAML | `~/.config/verdict/verdict.yaml` (or `$XDG_CONFIG_HOME/verdict/verdict.yaml`) | `primary_model`, `providers`, `gateway_url`, `gateway_kind` written by `verdict setup` / detect |
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
gateway_url: http://127.0.0.1:20128
gateway_kind: omniroute   # only when setup detected OmniRoute
```

Notes:

- `primary_model` is a preferred identity used when resolving defaults. It is
  **not** a silent fallback when eligibility returns an empty set.
- Live low-criticality `verdict route` admits a concrete **free-tier ∩ active**
  identity and fails closed with a named receipt when the intersection is empty.
  See [`guides/free-tier-admit-smoke.md`](guides/free-tier-admit-smoke.md).

### OmniRoute advisory rankings (`gateway_kind`)

`gateway_kind` tells Verdict which kind of gateway `gateway_url` points at. Only
`omniroute` changes behaviour: it lets the eligibility ladder fetch OmniRoute's
free-provider rankings (`/api/free-provider-rankings`, an OmniRoute management
endpoint) for `coding`, `debugging` and `review` selections, such as
`verdict choose`.

- **Read from:** `VERDICT_GATEWAY_KIND` when set and non-empty, else
  `gateway_kind` in `verdict.yaml`. The value is trimmed and case-insensitive.
  There is no `LLMGATE_` alias.
- **Default:** unset. Verdict then treats the gateway as a generic
  OpenAI-compatible gateway: it sends no rankings request and records the
  omission `generic_gateway`. `VERDICT_GATEWAY_KIND=generic` forces this even
  when the config file says `omniroute`.
- **Setup:** a fresh `verdict setup` writes `gateway_kind: omniroute` when the
  gateway it detects and saves as `gateway_url` identifies as OmniRoute. Setup
  leaves an existing `verdict.yaml` untouched, so on an existing install add the
  line yourself.
- **Gateway queried:** the same binding `verdict choose` uses:
  `OMNIROUTE_BASE_URL`, else the built-in `http://127.0.0.1:20128`. It does not
  read `gateway_url` from `verdict.yaml`.
- **Doctor:** `verdict doctor` prints an `Advisory rankings` line with the
  effective value, its source and the gateway it queried. It reads `active`
  only when the kind is `omniroute` and a bounded `coding` rankings fetch
  returned ranked rows; otherwise it reads `inactive` with the reason. This line
  is never an issue and does not change the exit code.

Rankings are **advisory only**. They reorder candidates that already passed hard
admission (authentication, entitlement, capability, quota, capacity,
availability, authority and risk). They never admit, exclude or entitle a
route, so a top-ranked route that admission excluded still cannot be selected.

How rankings order candidates:

- A score applies only to the exact `provider/model` route OmniRoute names (or
  a trusted canonical alias). It never transfers to other models of the same
  provider.
- Ranked routes get rank 1, 2, … by descending OmniRoute score. The rank is a
  tie-break: it sorts after capacity tier, subscription token usage, task
  rating, declared tier and capability slack, and before quota, provider
  preference, load and fit.
- **Tie-break:** when two admitted routes tie on every earlier key, a ranked
  route goes ahead of an unranked one. Unranked routes keep their baseline order
  relative to each other.
- A missing, 404, timed-out, malformed or stale (older than 24 hours) response
  leaves the whole order at baseline and records a named omission. A missing
  score stays unknown; it is never treated as zero.

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
| `log_path` | config file `log_path` | env `LLMGATE_LOG_PATH` | `$VERDICT_HOME/verdict-decisions.jsonl` (default `~/.verdict`) |
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

#### Decision log location

The routing decision log defaults to `$VERDICT_HOME/verdict-decisions.jsonl`
(`~/.verdict/verdict-decisions.jsonl` when `VERDICT_HOME` is unset). Earlier
releases wrote `./verdict-decisions.jsonl` in the working directory.
`LLMGATE_LOG_PATH`, the config file `log_path` and `--log_path` override the
default.

The readers (`verdict stats`, `verdict suggest`, `verdict cost-report`,
`verdict ui`) open `--log_path` when given, then `LLMGATE_LOG_PATH`, then the
state-directory log. When only a legacy `./verdict-decisions.jsonl` exists they
read it. When both exist they read the state-directory log and print one line on
stderr, once per process, naming the legacy log and how to read it:
`verdict stats --log_path ./verdict-decisions.jsonl`,
`verdict suggest --log_path ./verdict-decisions.jsonl`, or
`LLMGATE_LOG_PATH=./verdict-decisions.jsonl` for any command (`cost-report` and
`ui` have no flag). Nothing is printed when a path was given. The two logs are
never merged or migrated.

#### `verdict serve` precedence

`verdict serve` resolves three fields on its own, because a long-running server
must not change identity, profile or decision log the moment a routing config
appears on disk:

| Field (serve path only) | 1st | 2nd | 3rd |
|---|---|---|---|
| `primary_model` | env `LLMGATE_PRIMARY` | `verdict.contracts.DEFAULT_PRIMARY_MODEL` | — (routing config not read) |
| `profile` | env `LLMGATE_INTELLIGENCE_PROFILE` | `development` | — (routing config not read) |
| `log_path` | env `LLMGATE_LOG_PATH` | `$VERDICT_HOME/verdict-decisions.jsonl` | — (routing config not read) |

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
- A diagnostic line reads
  `<code> [<class>] field=<field> [source=<source>] - <detail>; remediation: <remediation>`.
  The field is followed by ` - `, never by
  `:` or `=`, because the field can be a credential name (`credential_env_missing`
  names `OMNIROUTE_API_KEY`). `NAME: word` is how a real key is written, so
  `verdict.security.redact_text` redacts it, and `scripts/check_dod.py` refuses a
  raw output that the redactor would change. The CLI notes, `verdict serve`
  startup lines, `describe_bootstrap_failure`, `BootstrapError` messages and the
  Prime supervisor's launch detail all use this format.
- The line also passes its field, detail and remediation through `redact_text`
  one part at a time, so an `sk-` key, a `Bearer` token or a `token=<value>` that
  reaches a detail by mistake is printed as `[redacted]`. A value with no label
  and no known prefix is not caught that way. The guarantee for those is that no
  diagnostic is built from a credential value.
  `tests/test_bootstrap_diagnostics_carry_no_credential_values.py` checks it with
  fake values for the registered credential names and the gateway key names, in the
  environment and in the credential store. Its docstring lists where each value goes
  and which names it leaves unset on purpose.
- A URL that carries `user:password@` userinfo is rendered as `***:***@host`
  everywhere: diagnostic details, `ProviderBinding.to_dict`,
  `BootstrapResult.to_dict`, `describe_bootstrap_failure` and the CLI/library
  stderr notes. The runtime binding keeps the operator's real URL, so execution
  is unaffected.
- `config_file_unparsable` reports the parser's problem plus the 1-based line and
  column. It never embeds the offending source line, which could itself hold a
  credential.

---

## Model metadata refresh

`verdict metadata refresh` updates Core's metadata store. The OpenRouter mirror
of Artificial Analysis scores is fetched only with `--include-p1`; unavailable
sources remain explicit and do not create ratings. Partial source failures retain
last-good fields with their original provenance.

The optional user timer runs `metadata refresh --include-p1` five minutes after
boot and every six hours after completion. `verdict setup` asks for explicit opt-in
before installing it. To preview, install, or remove the timer directly:

```bash
verdict metadata install-service --dry-run
verdict metadata install-service
verdict metadata install-service --uninstall
```

Dry-run renders the units without writing files or calling systemd. Repeated
installation with unchanged units leaves them untouched. `verdict doctor`
reports the uninstalled optional timer as an explicit opt-in warning. Once
installed, inactive timers, failed or never-completed refreshes, and stale source
ages fail the check. Completion or source evidence older than twelve hours fails;
missing, invalid, or future source timestamps remain explicit failures.

## PR watcher

The PR watcher polls open pull requests across configured repositories, classifies
CI status, and emits bounded reruns and transition events.

### Repository configuration

Repositories are configured via `pr-watch.env` at `<XDG_CONFIG_HOME or ~/.config>/verdict/pr-watch.env`
with a single line:

```
VERDICT_PR_WATCH_REPOS=owner/a,owner/b
```

Owner-only names are allowed (e.g., `mrnicholasbcarter-code/verdict-core,mrnicholasbcarter-code/verdict-node`).
The file must be owned by the user and not group/world-writable (mode 0600 recommended).
Surrounding quotes, an `export ` prefix, and leading spaces are stripped.
Each entry is validated as `^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$` without `..`.

### Timer

`verdict setup` installs and enables a user timer (`verdict-pr-watch.timer`) that runs
the watcher every five minutes. `verdict doctor` reports the timer state, runtime
provenance, configured repositories, and state scope with secrets redacted.
The watcher is opt-in: until `pr-watch.env` exists, a missing timer is a doctor
warning ("not configured"), not a failure. Once `pr-watch.env` exists, a missing,
inactive or failed timer fails doctor.

## Passive health refresh

The passive identify unit (`verdict-identify.service`, every three hours) runs
`verdict identify --passive --json --save-report --refresh-health-cache`. Besides
saving `identify-latest.json`, it folds real OmniRoute traffic into
`health-cache.json`. It makes no model calls and sends no writes to OmniRoute.

Sources:

- The OmniRoute health matrix (required). An entry's evidence age is the
  route's OmniRoute `lastRequestAt`, not the time of the refresh.
- Signed `request.completed` and `request.failed` webhook events, when a
  webhook is registered. Webhooks only add evidence.

Rules:

- Traffic writes chat-only liveness: healthy, `chat_ok` true, `tool_ok` false.
  It is never tool or agentic proof, and it never extends or replaces a
  usable tool proof.
- Entries carry `evidence_source` `omniroute_traffic` or `omniroute_webhook`.
  Probe entries have none.
- Freshness is the normal evidence lifetime: six hours plus a 1200-second
  grace. Older and future-dated traffic is skipped.
- Newer evidence wins. Traffic never overwrites a newer entry, and it never
  shortens an unexpired negative.
- Liveness is a scheduling hint, not admission evidence. A route it marks
  healthy still passes the exact live confirmation before launch. It can move
  a route earlier in the confirmation order and defer at-rest chat probes for
  that route while the entry is usable; agentic screening is unaffected.
- Only HTTP 402 and 5xx traffic becomes a route negative; authentication and
  request errors stay out of the cache, the same rule the relay uses.
- A webhook completion that reports zero output tokens is not liveness
  (`skipped_zero_output`). Traffic never turns a payment negative healthy,
  even after its backoff has ended (`skipped_billing_negative`): a provider
  can send a credit refusal as HTTP 200, and only a probe reads the reply.
- The webhook receiver is exempt from bearer auth because each delivery is
  HMAC-signed. It rejects bodies over 64 KiB with 413.

Webhook registration: `verdict identify register-webhook` registers Verdict's
receiver (`POST /v1/omniroute/webhook` on `verdict serve`) with OmniRoute.
OmniRoute refuses private or loopback webhook URLs unless its operator sets
`OMNIROUTE_ALLOW_PRIVATE_PROVIDER_URLS`; Verdict reports such a refusal (for
example `blocked_private`) and never changes that setting.

`verdict doctor` reports:

- **Identify passive report**: fails when the saved report has no
  `health_refresh`, or when the refresh has source errors, model calls or
  gateway writes. The ok row shows the recorded count and newest evidence age.
- **Health cache**: fails when no route is fresh or stale, and names the
  passive source, when it last refreshed, and the repair
  `systemctl --user start verdict-identify.service`. The ok row counts
  passive traffic, webhook and probe entries with their newest ages.
- **OmniRoute webhook**: ok when registered and an event arrived inside the
  usable window; otherwise a warning (never a failure) with the registration
  reason and the repair `verdict identify register-webhook`.

## Environment variable overrides

### Gateway / OmniRoute

| Variable | Meaning |
|----------|---------|
| `OMNIROUTE_BASE_URL` | Base URL of the local gateway (inventory/execute/health) |
| `OMNIROUTE_API_KEY` | API key/token for the configured gateway |
| `VERDICT_GATEWAY_KIND` | Gateway kind; `omniroute` enables advisory rankings. Overrides `gateway_kind` in `verdict.yaml`. See [OmniRoute advisory rankings](#omniroute-advisory-rankings-gateway_kind) |
| `VERDICT_OMNIROUTE_API_KEY` | Gateway key for `verdict orchestrate` and the OCR reviewer; usually the same value as `OMNIROUTE_API_KEY` |
| `OMNIROUTE_MANAGEMENT_TOKEN` | Management-plane credential: an access token (`oma_…`, Settings → Access Tokens; a `read` scope is enough for Verdict) or an `sk-` API key that has the `manage` scope. A plain inference key without `manage` is rejected. Serve, doctor, passive identify, subagent selection and the worker runtime read the environment first (a blank value is ignored), then the same-named field in the credential store. `verdict doctor` checks the token against OmniRoute and names its source |
| `OMNIROUTE_ALLOW_PRIVATE_HOSTS` | Comma-separated exact private-host allowlist, not a boolean; merged with the API upstream allowlist and loopback defaults |
| `OMNIROUTE_USAGE_API_KEY_ID` | Id (not secret) of the inference key whose usage/budget Verdict reads; from `/api/keys` |
| `VERDICT_GATEWAY_START_COMMAND` | Explicit command that starts a local gateway (shlex-split into an argv list; never run through a shell; the launched gateway inherits the exported environment) |
| `VERDICT_GATEWAY_READY_TIMEOUT_S` | Readiness budget in seconds for the bounded gateway wait (default `30`; must be finite, positive and at most `600`) |
| `VERDICT_ENSURE_GATEWAY` | Opt in to ensuring the gateway is ready before CLI execution (default off) |
| `VERDICT_SERVE_ENSURE_GATEWAY` | Opt in to ensuring the gateway is ready at `verdict serve` startup (default off) |

OmniRoute is **never** Verdict's model-metadata source of truth. Core metadata
comes from models.dev + LiteLLM (`verdict/metadata/`). See [ADR-032](adr/ADR-032-core-model-metadata-store.md).

### Serve / upstream proxy (`verdict serve`)

| Variable | Meaning |
|----------|---------|
| `LLMGATE_AUTH_TOKEN` | Bearer token for protected endpoints. Required: without it (and without `LLMGATE_ALLOW_ANONYMOUS`) `verdict serve` refuses to start. See [Serve authentication and receipts](#serve-authentication-and-receipts) |
| `LLMGATE_ALLOW_ANONYMOUS` | `1` allows an unauthenticated server, loopback hosts only, with a non-durable in-memory receipts store. Development only |
| `VERDICT_RECEIPTS_DB` | Durable receipts SQLite path. Required with `LLMGATE_AUTH_TOKEN` (ADR-017); no default is assumed |
| `LLMGATE_PRIMARY` | Preferred upstream identity string |
| `LLMGATE_UPSTREAM_BASE_URL` | Upstream base URL (defaults may derive from OmniRoute) |
| `LLMGATE_UPSTREAM_API_KEY` | Upstream API key |
| `LLMGATE_UPSTREAM_TIMEOUT_MS` | Upstream request timeout |
| `LLMGATE_UPSTREAM_ALLOW_PRIVATE_HOSTS` | Comma-separated exact private-host allowlist, not a boolean. API loopback defaults: `127.0.0.1`, `::1`, `localhost` |
| `LLMGATE_MODEL_ALLOWLIST` | Comma-separated allowed model ids |
| `LLMGATE_MODEL_DENYLIST` | Comma-separated denied model ids |
| `LLMGATE_INTELLIGENCE_PROFILE` | Intelligence profile. Default `development` (`verdict.intelligence.DEFAULT_PROFILE`). `production` makes every `IntelligenceService.route` caller (CLI `route`, supervisor) require an execution-path authority `ExecutionPathDecision` (`serve_path_authority_required`), makes `VERDICT_ALLOW_UNVERIFIED_DEV` inert, and makes `/v1/route/explain` exclude unverified candidates. The default is kept `development` on purpose: fail-closed admission does not depend on it (unknown/error/timeout candidates are excluded unless `VERDICT_ALLOW_UNVERIFIED_DEV=1`), and `/v1/route` forces execution-path authority in every profile (the OpenAI-compatible relay surfaces do not; see [Admission boundary](#admission-boundary)) |
| `LLMGATE_INTELLIGENCE_TIMEOUT_MS` | Intelligence timeout |
| `LLMGATE_ALLOW_CLIENT_MODEL_OVERRIDE` | Allow client-requested model override |
| `LLMGATE_LOG_PATH` | Decision log path |
| `LLMGATE_DISCOVERY_TTL_SECONDS` | Discovery TTL |
| `LLMGATE_FRONTIER_ALLOWLIST` | Comma-separated frontier allowlist |
| `LLMGATE_AVAILABILITY_TTL_SECONDS` | Availability cache TTL |
| `LLMGATE_AVAILABILITY_STALE_WINDOW_SECONDS` | Availability stale-while-revalidate window |
| `VERDICT_ALLOW_UNVERIFIED_DEV` | Opt-in (`1`/`true`) to admit unverified (unknown/error/timeout) candidates for non-protected work when the intelligence profile is `development`. Default off: unknown-state models are excluded as `runtime_truth_absent` |
| `VERDICT_OUTCOME_SHADOW_ROOTS` | BOD-304/BOD-426 outcome-shadow run directories (OS path-separator list, or one manifest file with one run dir per line). Default unset: shadow disabled and nothing persisted. When set, every admission receipt's `outcome_shadow` block is also appended to `outcome_shadow.jsonl` next to the first entry; see [Outcome shadow sink](#outcome-shadow-sink) |
| `VERDICT_WORKER_IDLE_SECONDS` | BOD-312 idle-output bound for `verdict orchestrate` Prime headless workers: a worker that writes nothing to stdout or stderr for this many seconds has its process group killed, the attempt ends `stalled`, and recovery moves the node to a different route within the attempt budget. Default `300`; an empty, zero, negative or non-numeric value keeps the default |

#### Serve authentication and receipts

`verdict serve` fails closed. It needs both of these before it starts:

- `LLMGATE_AUTH_TOKEN`: the bearer token clients send.
- `VERDICT_RECEIPTS_DB`: the durable receipts file. ADR-017 requires it in
  authenticated mode, so Verdict does not pick a path for you.

If either is missing, `verdict serve` prints one line naming the setting and the
fix, and exits 2. The simplest fix is `verdict setup --apply --allow host.serve`:
it installs the `verdict-serve` user unit and writes both settings to
`~/.config/verdict/serve.env` (mode 0600), with the receipts file at
`$VERDICT_HOME/receipts.db`. To run it by hand:

```bash
export LLMGATE_AUTH_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export VERDICT_RECEIPTS_DB="$HOME/.verdict/receipts.db"
verdict serve --host 127.0.0.1 --port 8000
```

For local development only, `LLMGATE_ALLOW_ANONYMOUS=1` starts without a token
on a loopback host and keeps receipts in memory (lost on restart).

### Outcome shadow sink

The candidate pool attaches a shadow-only `outcome_shadow` block to every
admission receipt (BOD-304). It never changes the live shortlist or choice.
`VERDICT_OUTCOME_SHADOW_ROOTS` turns it on and also picks where the blocks are
persisted (BOD-426):

- **Roots.** The value is an OS path-separator list of verified run
  directories (each with `receipt.json` and `events.jsonl`), or a single
  manifest file listing one run directory per line (`#` starts a comment).
- **Sink path.** `outcome_shadow.jsonl` in the directory that contains the
  first entry: the runs directory for a run-dir entry, or the manifest's own
  directory for a manifest file. For example,
  `VERDICT_OUTCOME_SHADOW_ROOTS=$HOME/.verdict/shadow-roots.txt` writes
  `$HOME/.verdict/outcome_shadow.jsonl`. The sink is never written inside a
  run directory, so receipt verification of the roots is unaffected.
- **Format.** One JSON object per receipt and per line:
  `{"schema": "verdict.outcome_shadow.sink/v1", "recorded_at": "...Z",
  "outcome_shadow": {...}}`. The file is append-only: it is opened with
  `O_APPEND` (and `O_NOFOLLOW`, so a symlinked sink is refused), created with
  mode `0600`, and each record is written as one complete line in a single
  `write` call.
- **Missing directory.** The sink directory is never created. If it does not
  exist, or the append fails, a warning is logged on `verdict.outcome_shadow`
  and admission carries on unchanged.
- **Unset.** No roots and no sink; nothing is written.

`would_rank` is only ever a computed rerank of the live shortlist
(`would_rank_scope: "live_shortlist_only"`). When no counterfactual was
computed (`metric_missingness` is `no_roots_configured` or `shadow_error`),
`would_rank` is `null` and `rank_delta` is empty. Readers must skip those
records rather than count them as agreement. `build_outcome_shadow` raises
`OutcomeShadowNotConfiguredError` if it is called with no roots.

The variable is read when the service starts, so a running `verdict serve`
needs a restart to pick up a change.

### Session evidence (orchestration, phase 1)

`SessionLedger` stores controller-verified worker-session outcomes separately
from probe health. Its default path is `$VERDICT_HOME/session-evidence.jsonl`
(`~/.verdict/session-evidence.jsonl` when `VERDICT_HOME` is unset).
`VERDICT_SESSION_EVIDENCE` overrides that path. An explicit constructor path
wins over the environment. Reading an absent ledger does not create it.

Attach it with `EligibilityLadder(..., session_ledger=SessionLedger(path))`.
The default ladder has no session ledger attached. For FREE implementation
routes, the existing fresh agentic-probe requirement still applies. Verified
outcomes in the last 14 days additionally require a Laplace score of at least
0.5. A false success claim counts as two failures and blocks the route for
7 days. No outcomes in the window leaves the probe-only behavior unchanged.

`import_worker_outcomes(path)` reads trusted controller JSONL and returns
outcomes without writing. Append its results to the ledger to import them.
Route, timestamp and outcome identify duplicates. Worker self-reports must not
be passed off as controller-verified evidence.

Eligibility JSON rank components expose `session_score`, `session_passes`
and `session_fails`. They are null when no ledger is attached. Phase 1 does
not change the sort order or automatically attach a ledger to the CLI.

### Admission boundary

Controller and worker launches start from one canonical live admission set
(`verdict/admission.py`). It is built from live inventory, provider connections
and persisted runtime evidence before any ranking. Later stages can only narrow
it. Catalog presence and an active connection are never enough on their own. A
route with no runtime evidence stays `unknown`: it can get a bounded probe, but
it is never recorded as healthy. Worker admission also hard-excludes the active
controller, which the supervisor exports as `VERDICT_ACTIVE_CONTROLLER_ROUTE`.
When that identity is not set, the receipt records `controller_identity=unknown`.

Launch gate. A route may launch only when its admission record is proven
healthy, or when a bounded live confirmation for that exact route succeeded
just before launch. A runtime source that was not found (for example a missing
`~/.verdict/orchestration-health.json` or `subagent-health.json`) is recorded
as `<source>:absent` and does not count as consulted. On a fresh host every
route is therefore `unknown` and needs a confirmation. A failed confirmation
drops the route at `HEALTHY` with the probe category and never launches.
Confirmation results (source and `observed_at`) are written to the admission
receipt.

Operator-visible fail-closed modes:

- Config-built root controller (`scripts/prime_supervisor.py`): each attempt
  makes two read-only GETs, `/v1/models` and `/api/providers`, against
  `OMNIROUTE_BASE_URL` (default `http://127.0.0.1:20128`). If no admission
  source is wired, the supervisor refuses with `live_admission_required`. If
  either GET fails, it refuses with `live_admission_unavailable`. In both
  cases `supervisor.json` is `BLOCKED`. The receipt is
  `controller-admission-*.json` in the state directory.
  A controller route-prefix scope can be set with `--scope` (flag) or
  `VERDICT_CONTROLLER_ROUTE_PREFIXES` (env var, comma-separated). When set,
  only admitted routes whose id starts with one of the listed prefixes are
  eligible; the stage is `CONTROLLER_SCOPE` with reason
  `outside_controller_route_prefix`. If the scope is non-empty but no admitted
  route matches, the supervisor fails closed with `controller_scope_empty`.
- `verdict supervise` worker containment: pass `--cgroup-root PATH` or set
  `VERDICT_CONTROLLER_CGROUP_ROOT` to a writable delegated cgroup-v2 subtree.
  `--cgroup-root auto` uses the supervisor's own scope when it is delegated, e.g.
  `systemd-run --user --scope -p Delegate=yes verdict supervise --cgroup-root auto ...`.
  Without a root the command exits 2 with these instructions and creates no run state.
- Controller confirmation: the controller launches only routes that the
  budgeted confirm in `prepare` reported as confirmed, or routes that are
  proven healthy. Otherwise it refuses with `no_eligible_route`, and the
  detail names each route's stage, reason and source.
- Worker runtime (`python -m verdict.worker_runtime <dir>`): besides inventory
  and Prime visibility, it now needs `GET /api/providers`. If that call fails,
  the run exits 1 with `connection_evidence_unavailable`. A route with
  `unknown` health gets one health probe before spawn, even when the local
  health cache says it is healthy. The result is written to `admission.json`
  in the run directory.

### Public route endpoint (`POST /v1/route`)

`POST /v1/route` is the authoritative routing endpoint. It requires an
`execution_path_request` body (schema version `execution-path/v1`) describing
candidate offers with pricing, capability evidence, and certification state.
A request without `execution_path_request` returns HTTP 400.

#### Execution-path request fields

The `execution_path_request` object accepts the following fields (in addition
to the required `trajectory_id`, `slice_id`, `candidates`, `acceptance_criteria`,
`proof_criteria`, and optional `task_family`, `min_context_tokens`,
`reasoning_required`, `tools_required`, `structured_output_required`,
`streaming_required`, `vision_required`):

| Field | Type | Required | Description |
|---|---|---|---|
| `hard_spend_budget` | number (USD) | No | Hard spend ceiling in USD. Must be a finite JSON number > 0. Absent = no budget enforcement (existing behaviour). Explicit `null`, boolean, string, array, object, zero, negative, non-finite, or overflow = HTTP 400 naming `execution_path_request.hard_spend_budget`. When set, offers with `expected_cost.cash_usd > budget` are rejected with `exceeds_hard_spend_budget`; offers with unknown cash (`has_unknown_cash`) are rejected with `unknown_price_not_treated_as_free`. Exact equality (`cash_usd == budget`) qualifies if otherwise eligible. No budget = unknown price remains advisory (A25 invariant). |

#### Query parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `include_routing_trace` | boolean | `false` | When `true`, the response includes a `routing_trace` object with structured diagnostics: `admitted_candidate_ids` (set), `shortlist_candidate_ids` (ordered), `optimizer_decision` (`outcome` + `selected_candidate_id`), `rejected_offers` (`candidate_id` + `code`). The trace is a read-only projection of the same request's admission, shortlist, and optimizer stages. Present on all authoritative outcomes including no-selection (`denied`, `blocked`). Legacy response (without this parameter) is unchanged. |

#### Response

On success (HTTP 200): a routing decision object with `model`, `provider`, `tier`, `reason`, `alternatives`, `decision` ("selected" or "denied"), `candidate_states`, `admit_receipt`, and evidence headers (`x-verdict-evidence-id`, `x-verdict-evidence-request-id`, `x-verdict-correlation-id`).

When `include_routing_trace=true`, an additional `routing_trace` field is included (see above).

On denial (HTTP 503): a routing decision with `decision: "denied"` and `model: null`.

On invalid request (HTTP 400): error detail naming the invalid field (e.g., `execution_path_request.hard_spend_budget`).

The OpenAI-compatible relay (`/v1/chat/completions`, `/v1/responses`) has a
narrower boundary:

- In the default `development` profile, the relay is **not** under
  authoritative live admission unless `verdict.api.relay_admission_provider`
  is wired. It defaults to `None`.
- When the provider is wired, the selected model and every alternative must be
  in the admitted set. A provider error returns 503 before any upstream call.
- With `VERDICT_RELAY_ADMISSION_CACHE=true`, the API lifespan task reloads the
  provider from local stores every 20 s (5 s deadline) and the 60 s cache TTL is
  capped by the source evidence deadline (age < 600 s). `GET /v1/models` is the
  local verified projection (fresh positive proof only, no gateway or model
  call) and returns 503 with a classified reason when the inventory is absent,
  corrupt, incomplete or expired.
- Relay alternatives are admitted-only in every mode. An alternative is tried
  only when the decision carries `admitted: true` candidate state for it.
  Without that state, alternatives are dropped, not treated as eligible.

### Request spend policy

A request may set `spend_policy` (body field on `/v1/chat/completions`, or the
routing context). Verdict imposes no price policy of its own: when the field is
absent, the policy is `paid_allowed` and every admitted, proven route competes
on capability. Explicit values are caller filters, like `--capacity`:

| Value | Effect |
|---|---|
| `paid_allowed` (default) | No price filter or price ordering. |
| `free_preferred` | Everyone competes; the first capability-ranked free route is chosen when one is admitted. |
| `free_only` | Non-free routes are excluded with `paid_model_not_allowed`. |
| `frontier_required` | Only non-free frontier-class (or allowlisted) routes remain; others get `spend_policy_requires_frontier`. |

Free membership comes only from OmniRoute free-tier metadata (`free_admitted`),
never from a model name. Unknown values are refused. The applied policy is
recorded in the admit receipt (`spend_policy`).

### Receipts / evidence

| Variable | Meaning |
|----------|---------|
| `VERDICT_RECEIPTS_DB` | Durable API evidence SQLite path (required for authenticated API mode; `:memory:` selects an explicit non-durable store, as the test suite does) |
| `VERDICT_EVIDENCE_DB` | Legacy alias for the durable evidence path |
| `VERDICT_MEMORY_DB` | MemoryPlane SQLite path (failover/replay demos) |

### Optional platform-neutral guidance

Guidance is an experimental, host-neutral boundary. It is disabled by
default, does not read `AGENTS.md` / `CLAUDE.md`, and cannot bypass gates. <!-- claim:C-048 -->

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

## Installed-runtime provenance and the maintenance refresh lane

`verdict doctor` reports one row per loaded long-running service unit (prove-at-rest,
census, acceptance, prime-sync, metadata-refresh): the package its `ExecStart`
interpreter imports from the unit's working directory, that package's recorded
build, the unit's gateway origin, and its effective state scope (`VERDICT_HOME`).
A row FAILS whenever the interpreter's own install, or the imported package, is
not the reviewed build (including an unrecorded build, which is never reported
OK even when the install path looks right), the state scope differs, or
`VERDICT_HEALTH_CACHE` points at a different cache. The gateway origin shown is
always reduced to `scheme://host:port` (plus a literal `/...` marker for a path);
userinfo, query strings and fragments never appear in the detail text, because
they can carry tokens.

Every service installer (prove-at-rest, acceptance, census) returns an install
`plan` (per-unit create/replace/keep, or remove/absent on uninstall) and writes a
`rollback` snapshot of every unit file it is about to replace or remove, under
`$VERDICT_HOME/service-rollback/<UTC>/`, before any unit file is written. A dry
run returns the plan and writes nothing.

Census and prove-at-rest's chat phase are the two automatic maintenance <!-- claim:C-047 -->
producers on the shared `RefreshCoordinator` (`verdict/orchestration/verified_refresh.py`).
Both probe FREE and SUBSCRIPTION capacity only; METERED/UNKNOWN routes stay
reachable only through the consented manual refresh plan (`--include-unknown`,
`--allow-metered`), never from an automatic cycle. Census keeps its existing
4-route/16-request/2-per-provider maintenance size. Prove-at-rest's chat phase
computes its own bounded config from the daemon's own flags: `max_routes =
max(1, min(daemon --max-requests // 2, 50))`, `max_requests = min(daemon
--max-requests, 100)`, `wall_seconds = min(daemon --max-wall-seconds, 600)`.
Neither producer exceeds the coordinator's hard maxima
(`HARD_MAX_ROUTES=50`, `HARD_MAX_REQUESTS=100`,
`HARD_MAX_WALL_SECONDS_MAINTENANCE=600`). Both go through the same flock,
marker, bucket and `auth_gate` machinery as every other coordinator caller; a
budget too small for one full probe makes zero calls (no partial-probe spend).

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
a frontier model when eligibility returns nothing. <!-- claim:C-049 -->

## Live gateway checks

Only run these when a compatible gateway is already running at `http://localhost:20128`:

```bash
verdict detect --json
verdict probe task-coding --base-url http://localhost:20128/v1 --allow-live-probe --json
```

`detect` must show `server_running: true`. `probe` must return `status: ready` for a
**named** model. `auto/*` IDs are opaque and are not live proof. A catalog timeout is
`blocked`, not success. See [`docs/guides/golden-path.md`](guides/golden-path.md) for the
dated live observation and its limitations. <!-- claim:C-026 --><!-- claim:C-027 -->

With `OMNIROUTE_BASE_URL` (and `OMNIROUTE_API_KEY` when required) a low-criticality `verdict
route` admits a concrete **free-tier ∩ active-provider** identity, prints an `admit_receipt`
of named drops, and executes through `/v1/chat/completions`. An empty intersection fails
closed instead of falling back to Opus. See
[`docs/guides/free-tier-admit-smoke.md`](guides/free-tier-admit-smoke.md). Keep those
identities proved in the background with
[`verdict prove-at-rest`](guides/health-cache.md) (every admitted route;
the health cache is separate from selection, which is unchanged).

## Capacity classification

Verdict classifies route capacity into `FREE`, `SUBSCRIPTION`, `METERED`, and `UNKNOWN` from facts
only, never from a name (operator, 2026-11-19). A route is `FREE` on an explicit 0/0 inventory price
(rule 0.2, `verdict/omniroute_pipeline/free_rule.py`), OpenRouter public 0/0 membership, the
gateway's free-tier membership, or a connection free signal (`importFreeModelsOnly`, a free plan or
tier). oauth without a free signal is `SUBSCRIPTION`; a positive price or an observed billing 402
is `METERED`. A `:free` suffix or a provider name decides nothing: with no fact the route is
`UNKNOWN` (`no_free_evidence`), and unknown is never free.

---

## Related

- [Getting Started](GETTING_STARTED.md)
- [CLI Reference](CLI_REFERENCE.md)
- [Architecture](architecture.md)
- [`.env.example`](../.env.example) — commented inventory of supported env keys


## Restricted gateway connections snapshots

An inference-scoped OmniRoute key cannot read `/api/providers`.
Never send a manage/admin key through a public tunnel.
Capture connection evidence locally, using the local admin key: <!-- claim:C-050 -->

```bash
# Supply VERDICT_OMNIROUTE_API_KEY securely in this local process.
verdict gateway connections-snapshot --out /tmp/connections.json
```

The command accepts only loopback gateways and disables redirects. It writes
sanitized connection facts, opaque identifiers, a capture time and a SHA-256
digest at mode `0600`. Plan labels become a fixed free-tier category; account
names, emails, keys and provider error text are not retained.

Set `VERDICT_CONNECTIONS_SNAPSHOT` to this file when using an inference key.
The orchestration connections fetch then makes no `/api/providers` request.
The snapshot must use schema version 1, match its digest, and be at most six
hours old. Future-dated or malformed evidence fails closed without HTTP fallback.
The digest covers canonical JSON of all fields except `sha256`, using sorted
keys and separators `(',', ':')`. It detects corruption, not malicious forgery.
The capture timestamp does not refresh individual quota or health evidence.
The file is trusted local input, not signed evidence. It feeds entitlement only:
exact live confirmation and the gateway key's model allowlist still gate launches.
Capture refuses proxy environment variables, so the admin key cannot leave loopback.
Unparseable cooldowns stay blocked until an operator corrects their source evidence.

For the protected `certification` environment, set `VERDICT_CERT_CONNECTIONS`
to the complete fresh snapshot JSON. The rehearsal script requires this secret,
masks its JSON, writes it privately under `RUNNER_TEMP`, and passes the snapshot
path to child processes. Missing or invalid snapshots stop before fixture work.
Use an inference-only, allowlisted, expiring and rate-limited tunnel key for
`VERDICT_CERT_GATEWAY_KEY`. The snapshot does not contain gateway credentials.
