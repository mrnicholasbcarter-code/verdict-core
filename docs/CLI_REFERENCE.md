# CLI Reference

```
verdict [global flags] <command> [args]
```


## Command map

The registered subcommands below are grouped by their primary use. Run
`verdict --help` for the authoritative list.

| Family | Commands |
|---|---|
| Setup and configuration | `setup`, `plan`, `check`, `doctor`, `compat`, `uninstall` |
| Routing and selection | `route`, `run`, `compare`, `choose`, `simulate`, `models`, `inspect`, `failover-proof`, `replay` |
| Orchestration | `orchestrate`, `supervise`, `watch`, `run-receipt`, `eligibility`, `resume` |
| Execution and service | `serve`, `detect`, `probe`, `catalog`, `prove-at-rest`, `certify` |
| Metadata, memory, and integrations | `metadata`, `memory`, `mcp`, `hook`, `harness`, `runtime` |
| Development workflows | `autodev`, `autodev-golden-path`, `quickstart` |
| Reporting and review | `stats`, `benchmark`, `cost-report`, `receipt`, `suggest` |
| Interface | `ui` |

## Bare `verdict` — Home screen

Running `verdict` with no subcommand opens the home screen. It exits successfully
without selecting or executing a model.

```bash
verdict
```

## Orchestration commands

### `verdict orchestrate` — Run a goal through a DAG to a receipt

```bash
verdict orchestrate "Implement the feature" --repo . --json
```

| Flag | Description |
|---|---|
| `goal` | High-level goal; omit when using `--resume` |
| `--repo REPO` | Git repository to change (default: `.`) |
| `--graph GRAPH` | Use a pre-built WorkGraph JSON instead of frontier planning |
| `--resume RUN_ID` | Resume a run from durable state |
| `--runs-dir RUNS_DIR` | Run directory root |
| `--gateway GATEWAY` | Gateway endpoint or identifier |
| `--max-parallel MAX_PARALLEL` | Maximum parallel workers |
| `--attempt-timeout ATTEMPT_TIMEOUT` | Per-attempt timeout |
| `--run-deadline RUN_DEADLINE` | Overall run deadline |
| `--prefer PREFER` | Comma-separated provider preference among subscription capacity (ranking policy, not a fallback chain) |
| `--scope SCOPE` | Comma-separated allowed route prefixes; empty means all |
| `--no-review` | Skip OCR review; run ends BLOCKED |
| `--inject ROUTE=FAULT[,FAULT]` | Inject a named chaos fault for a route |
| `--state-file STATE_FILE` | Health/cooldown state file (default `~/.verdict/orchestration-health.json`) |
| `--plain` | ASCII narrative instead of live view |
| `--json` | Print final receipt JSON |

### `verdict supervise` — Supervise and resume a run

```bash
verdict supervise --run-id RUN_ID --runs-dir .verdict/runs
```

| Flag | Description |
|---|---|
| `--run-id RUN_ID` | Run ID to supervise and resume (required) |
| `--runs-dir RUNS_DIR` | Directory holding run directories (required) |
| `--stall-seconds STALL_SECONDS` | No-progress limit |
| `--poll-seconds POLL_SECONDS` | Liveness poll interval |
| `--max-restarts MAX_RESTARTS` | Controller restart budget |
| `--total-deadline-seconds TOTAL_DEADLINE_SECONDS` | Hard deadline |
| `orchestrate_args` | Arguments after `--` passed to orchestration |

### `verdict watch` — View orchestration state

```bash
verdict watch RUN_ID --runs-dir .verdict/runs --once
```

| Flag | Description |
|---|---|
| `run` | Run ID or run directory |
| `--runs-dir RUNS_DIR` | Run directory root |
| `--once` | Render current state and exit |

### `verdict run-receipt` — Show and verify an orchestration receipt

```bash
verdict run-receipt RUN_ID --runs-dir .verdict/runs --json
```

| Flag | Description |
|---|---|
| `run` | Run ID or run directory |
| `--runs-dir RUNS_DIR` | Run directory root |
| `--json` | Print JSON |

### `verdict eligibility` — Show the eligibility ladder

```bash
verdict eligibility --frontier --json
```

| Flag | Description |
|---|---|
| `--gateway GATEWAY` | Gateway endpoint or identifier |
| `--scope SCOPE` | Comma-separated route prefixes to allow |
| `--prefer PREFER` | Provider preference |
| `--probe` | Probe lazily to reach `SELECTED` |
| `--reasoning` | Request reasoning-capable routes |
| `--frontier` | Request frontier-capable routes |
| `--provider-family FAMILY[,FAMILY]` | Only evaluate routes whose id prefix (before `/`) is listed, e.g. `cc,kr`; repeatable |
| `--json` | Print JSON |
| `--no-pager` | Never pipe human output through a pager |

Output is never truncated. `--json` returns every evaluated verdict plus
`filters` (active user filters), `evaluated_count` (= `len(verdicts)`), and a
`summary` whose `selected` equals the top-level `selected.route_id` and whose
`by_reached_stage` buckets sum to `evaluated_count`. Human output starts with a
`filters:` line, then lists every route: selected, then ranked/eligible, then
rejected grouped by failed stage. On a TTY it is piped through `$PAGER` (else
`less -R`).

The harness gate is the live gateway inventory (`GET /v1/models`, the same rows
the ladder discovers). Prime's `~/.prime/agent/models.json` is only a fallback.
With neither source available, every route is denied at `ENTITLED` with reason
`harness_inventory_unavailable` (fail-closed).

With `--probe`, the probe budget (8 per select) is spread round-robin across
providers inside each capacity class. A `payment_required`, `permission`, or
`authentication` probe failure sets a persisted `provider:<name>` cooldown. The
other routes of that provider are then skipped (`cooldown:provider`) without a
probe, in this select and in later ones. A 400 whose body says the id is "not
available in the active live catalog" is `unservable`, with a 6 h route cooldown.

### `verdict harness prime sync-models` — Sync Prime's OmniRoute model list

```bash
verdict harness prime sync-models --dry-run
verdict harness prime sync-models --gateway http://127.0.0.1:20128
```

Fetches the live `/v1/models` and rewrites only `providers.omniroute.models` in
`~/.prime/agent/models.json`. Existing per-model settings (for example
`thinkingLevelMap`) are kept. Before it writes, it copies the file to
`models.json.verdict-sync-<UTC stamp>.bak`. It prints the added and removed counts.

| Flag | Description |
|---|---|
| `--gateway GATEWAY` | Gateway base URL (default `$OMNIROUTE_BASE_URL`, then `:20128`) |
| `--dry-run` | Print the added/removed diff; write nothing |

---

## Global Flags

| Flag | Description |
|------|-------------|
| `-h, --help` | Show help |

---

## Commands

### `verdict route` — Select and execute a qualified model

```bash
verdict route "your task prompt" [flags]
```

| Flag | Description |
|------|-------------|
| `--terse` | On success, output only the selected model; failures remain structured and explicit |
| `--criticality <level>` | `low` \| `medium` \| `high` \| `critical` |
| `--allow-offline` | Disable network discovery/probes; does not enable the legacy selector |
| `--allow-legacy-selector` | Explicit BOD-127 migration escape for the pre-BOD-104 selector |

**Examples:**
```bash
verdict route "Write a Rust CLI tool" --terse
verdict route "Deploy to production" --criticality high
```

With a configured OpenAI-compatible provider, this command performs a completion rather than merely print a routing
forecast. `simulate` is the no-send forecasting command. For live OmniRoute
cheap-path execution, Verdict loads the current inventory, admits only named
free-tier identities on active providers, requires fresh prove-at-rest evidence,
runs a bounded confirmation probe, ranks the admitted set, compiles a bounded
provenance-aware context pack, and then sends `/v1/chat/completions`.

The output is intentionally explicit:

- `transport_outcome=not_sent` is not execution.
- An empty qualified intersection exits non-zero with `model=no_eligible_target`.
- Provider-reported model identity must match the selected identity; mismatch
  fails closed.
- HTTP success means only that transport succeeded. Quality remains `unknown`
  until a verifier records a quality outcome.

Use `verdict prove-at-rest once --allow-live-probe --json` to refresh health
evidence. `verdict prove-at-rest status --json` only reads the persisted cycle
and does not make network requests.

### `verdict run` — Execute through the route path

```bash
verdict run "Summarize the current diff" [--terse] [--criticality low|medium|high|critical]
```

`run` is the live alias of `route`, but it does not expose the route command's
offline or legacy-selector migration flags.

---

### Eligibility ranking and freshness

There is no standalone `verdict explain` command. Use `verdict models`,
`verdict inspect`, and `verdict prove-at-rest status --json`
to inspect selection, exclusions, and evidence freshness.

---

### `verdict models` — List available models

```bash
verdict models [flags]
```

| Flag | Description |
|------|-------------|
| `--json` | Output machine-readable JSON |

---

### `verdict metadata` — Core model metadata store (BOD-108)

Independent of OmniRoute. See [`guides/model-metadata-store.md`](guides/model-metadata-store.md).

```bash
verdict metadata refresh [--json]
verdict metadata show [--json]
verdict metadata lookup <omniroute_id> [--requires tools,vision,structured,context] [--json]
```

| Subcommand | Description |
|------------|-------------|
| `refresh` | Fetch models.dev + LiteLLM into `~/.verdict/model-metadata.json` |
| `show` | Summarize the on-disk store |
| `lookup` | Join one OmniRoute id; unmapped/required-unknown → named drop |

| Flag | Description |
|------|-------------|
| `--store <path>` | Store path |
| `--mapping <path>` | Explicit OmniRoute → models.dev map |
| `--models-dev-api-file <path>` | Offline models.dev `api.json` |
| `--litellm-file <path>` | Offline LiteLLM JSON |
| `--requires <caps>` | Lookup: comma-separated required caps |

---


### `verdict ui` — Launch the Streamlit analytics dashboard

```bash
verdict ui
```

Requires the dashboard extras (`pip install "verdict-core[dashboard]"` or
`"verdict-core[all]"`). The dashboard reads `verdict-decisions.jsonl` from the
current directory and joins it to `verdict-outcomes.jsonl` beside it. Spend is
shown only from those post-execution outcome receipts; any per-model price
assumption is an opt-in view explicitly labelled *synthetic* and is never
presented as measured spend.

#### Outcome receipts (`verdict-outcomes.jsonl`)

The decision log is written *before* the upstream call and therefore cannot
know what an execution cost. `verdict serve` writes one **outcome receipt**
per upstream attempt after the gateway answers, to a sibling file derived from
`log_path` (`verdict-decisions.jsonl` → `verdict-outcomes.jsonl`;
`custom.jsonl` → `custom-outcomes.jsonl`). Records join to decisions on
`request_id`. When a request has several attempts, the highest `attempt` is
the one the client received (identity, status), but **measured spend sums
every billed attempt** — a retry the gateway charged for is still money spent
serving that decision. A client-supplied `request_id` that appears on more
than one decision row is ambiguous: its receipts are excluded from measured
spend rather than credited to each row. The dashboard never offers an outcome
log as a decision log.

| Field | Source | Notes |
|-------|--------|-------|
| `record` | constant `"outcome"` | distinguishes rows from decision records |
| `schema_version` | constant `1` | |
| `request_id` | the decision's `request_id` | join key |
| `attempt`, `surface`, `status_code` | serve path | `surface` is `chat` or `responses` |
| `completed_with`, `completed_with_source` | `X-OmniRoute-Model` header, else the attempt's model | source is `header` or `attempt.model` |
| `execution_id` | `X-OmniRoute-Request-Id` | `null` when absent |
| `observed_cost_usd`, `cost_source` | `X-OmniRoute-Response-Cost` **only** | `null` when the header is absent — unmeasured, never `$0`; never estimated from a model name |
| `observed_tokens_in/out/total`, `tokens_source` | `X-OmniRoute-Tokens-In/Out`, else body `usage` (`prompt_/completion_tokens` for Chat Completions, `input_/output_tokens` for Responses) | token counts never become a price |
| `cache_hit` | `X-OmniRoute-Cache-Hit` | `null` when absent |

Streamed responses contribute headers only (the body is not buffered), so
their token counts come from headers or are `null`. Writing a receipt never
raises; if `log_path` is empty nothing is written.

---


### `verdict serve` — Launch FastAPI microservice

```bash
verdict serve [flags]
```

| Flag | Description |
|------|-------------|
| `--host <ip>` | Host (default: 127.0.0.1) |
| `--port <n>` | Port (default: 8000) |
| `--dev` | Enable hot-reload development mode |

---

### `verdict detect` — Detect available LLM providers

```bash
verdict detect
```

Scans for local providers (Ollama, LM Studio, etc.) and configured API keys.

---

### `verdict probe` — Run 1-token liveness probe

```bash
verdict probe <model_id> [<model_id> ...] --allow-live-probe [--json]
```

Network probes require the explicit `--allow-live-probe` consent flag.

---

### `verdict suggest` — Review intelligence suggestions

```bash
verdict suggest [flags]
```

---

### `verdict doctor` — Scan & repair config/connectivity

```bash
verdict doctor [flags]
```

| Flag | Description |
|------|-------------|
| `--fix` | Auto-fix issues |
| `--json` | Output a machine-readable report |
| `--preflight-timeout SECONDS` | Bound the documentation preflight (default `120`; `0` = unbounded) |

Text mode prints progress lines on stderr before and during the documentation
preflight (`checking documentation memory (may take a while)...`, then
`checking documentation source <id>...` and, under `--fix`,
`ingesting N/M documents (<source>: <path>)...`). `--json` prints no progress,
so stdout stays pure JSON. The deadline is checked between documents and never
interrupts a write. A preflight that hits the deadline reports
`documentation_preflight.timed_out: true`, status `blocked`, and an issue that
names `--preflight-timeout`. It is never reported as ready.

Exit code: `0` when the host is healthy, `1` when unresolved issues remain
(after `--fix` has been applied, if used). Text and `--json` modes run one
shared diagnostics collector (`_collect_doctor_diagnostics` in
`verdict/cli.py`), so they report the same `issues` and `warnings` and agree
on exit code for the same host state. Text mode renders the result; `--json`
serialises it (`status`, `issues`, `warnings`, `repaired`, `sections`,
`documentation_preflight`, `shared_memory`, `capability_bootstrap`,
`runtime_health`).

Issues (exit `1`):

- `verdict.yaml` missing, invalid YAML, or not a mapping.
- No `primary_model`; `providers` not a mapping.
- Literal secret (`sk-` / `api_key`) in a provider `base_url`; duplicate provider `base_url`.
- No `schema_version` (fixed by `--fix`).
- Legacy `config.yaml` (renamed by `--fix`), or both `config.yaml` and `verdict.yaml` present.
- No gateway URL, or gateway `/api/health` unreachable / not HTTP 200. On an
  old config with no `gateway_url`, `--fix` writes `gateway_url` only when
  exactly one healthy local gateway is detected (ports 20128/20129/20132). It
  also stores the same URL as `OMNIROUTE_BASE_URL` in the credentials store if
  that entry is missing. When no gateway or more than one is found, the issue
  stays.
- Malformed `OMNIROUTE_BASE_URL` (expected `http://host:port`, no trailing slash); `OPENAI_API_KEY` without the `sk-` prefix.
- Duplicate OmniRoute provider nodes; unreachable provider nodes.
- Missing required credential.
- Documentation preflight did not pass, unless it is network-only (see below),
  or timed out (`--preflight-timeout`).

Warnings (exit unaffected):

- `missing_mcp_config` (no `./.mcp.json`; `--fix` creates it).
- Memory bridge: `missing_memory_db`, `missing_memory_db_file` (`--fix` creates
  `~/.verdict/` and initializes `memory.db`).
- Documentation preflight network-only failure: `missing == 0`, `stale == 0`,
  `orphaned == 0`, and every error is a `resolve`/`inventory` fetch error that
  contains `rate limit`, `HTTP Error 429`, `URLError`, `timed out`,
  `Connection refused`, or `Name or service not known`. A bare
  `HTTP Error 403` (for example `Forbidden`) is an issue; a 403 counts as
  network-only only when the same error also says `rate limit`.

Text mode may prompt to delete duplicate OmniRoute nodes; `--json` never prompts.

---

### `verdict check` — Validate config syntax

```bash
verdict check
```

---

### `verdict setup` — Interactive setup wizard

```bash
verdict setup
```

---

### `verdict stats` — View routing analytics

```bash
verdict stats [flags]
```

| Flag | Description |
|------|-------------|
| `--log_path <path>` | Decision log to summarize |

---

### `verdict benchmark` — Run reproducible benchmark

```bash
verdict benchmark [flags]
```

| Flag | Description |
|------|-------------|
| `--fixture <path>` | Benchmark fixture |
| `--runs <n>` | Number of runs |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `XDG_CONFIG_HOME` | Config root; Verdict reads `$XDG_CONFIG_HOME/verdict/verdict.yaml` when set |
| `OMNIROUTE_BASE_URL` | OmniRoute endpoint |
| `LLMGATE_PRIMARY` | Primary model (legacy) |
| `LLMGATE_INTELLIGENCE_PROFILE` | Intelligence profile |
| `LLMGATE_LOG_PATH` | Decision log path |
| `VERDICT_RECEIPTS_DB` | Durable SQLite receipt database; required for authenticated API mode |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success |
| `1` | General error |
| `2` | Invalid arguments |
| `3` | Config / usage error |

The CLI raises `SystemExit(1|2|3)` only. There is no shipped exit code `4` or `5`.
Empty eligibility is reported in the receipt / command output and typically exits
non-zero via the general error path rather than a dedicated code.
