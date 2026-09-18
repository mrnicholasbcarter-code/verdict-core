# CLI Reference

```
verdict [global flags] <command> [args]
```

## Global Flags

| Flag | Description |
|------|-------------|
| `-h, --help` | Show help |
| `--version` | Show version |
| `--config <path>` | Config file path |
| `--verbose` | Verbose output |

---

## Commands

### `verdict route` — Route task to best model

```bash
verdict route "your task prompt" [flags]
```

| Flag | Description |
|------|-------------|
| `--terse` | Output model name only |
| `--verbose` | Show full reasoning |
| `--criticality <level>` | `low` \| `medium` \| `high` \| `critical` |
| `--context <json>` | Additional context for routing |
| `--policy <name>` | Policy name to use |

**Examples:**
```bash
verdict route "Write a Rust CLI tool" --terse
verdict route "Deploy to production" --criticality high --context '{"repo":"acme/api"}'
```

---

### `verdict explain` — Show eligibility ranking & freshness

```bash
verdict explain "your task prompt" [flags]
```

Shows candidate models, eligibility reasoning, freshness data, exclusion reasons.

---

### `verdict models` — List/refresh available models

```bash
verdict models [flags]
```

| Flag | Description |
|------|-------------|
| `--refresh` | Force refresh from OmniRoute |
| `--provider <name>` | Filter by provider |
| `--free-only` | Show only free tiers |

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

### `verdict policy` — Manage routing policies

```bash
verdict policy <subcommand> [args]
```

| Subcommand | Description |
|------------|-------------|
| `get [name]` | Show policy |
| `set <name> <file>` | Set policy from YAML/TOML |
| `validate <file>` | Validate policy syntax |
| `list` | List available policies |
| `delete <name>` | Delete policy |
| `explain <file>` | Evaluate a redacted policy fixture without execution |
| `simulate <file>` | Simulate policy and transition decisions offline |
| `backtest <file>` | Alias for deterministic offline simulation |

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
`request_id`; when a request has several attempts, the highest `attempt` is
the one the client received.

| Field | Source | Notes |
|-------|--------|-------|
| `record` | constant `"outcome"` | distinguishes rows from decision records |
| `schema_version` | constant `1` | |
| `request_id` | the decision's `request_id` | join key |
| `attempt`, `surface`, `status_code` | serve path | `surface` is `chat` or `responses` |
| `completed_with`, `completed_with_source` | `X-OmniRoute-Model` header, else the attempt's model | source is `header` or `attempt.model` |
| `execution_id` | `X-OmniRoute-Request-Id` | `null` when absent |
| `observed_cost_usd`, `cost_source` | `X-OmniRoute-Response-Cost` **only** | `null` when the header is absent — unmeasured, never `$0`; never estimated from a model name |
| `observed_tokens_in/out/total`, `tokens_source` | `X-OmniRoute-Tokens-In/Out`, else body `usage` | token counts never become a price |
| `cache_hit` | `X-OmniRoute-Cache-Hit` | `null` when absent |

Streamed responses contribute headers only (the body is not buffered), so
their token counts come from headers or are `null`. Writing a receipt never
raises; if `log_path` is empty nothing is written.

---

### `verdict config` — Manage local configuration

```bash
verdict config <subcommand> [args]
```

| Subcommand | Description |
|------------|-------------|
| `show` | Show effective config |
| `edit` | Open config in $EDITOR |
| `get <key>` | Get config value |
| `set <key> <value>` | Set config value |
| `reset` | Reset to defaults |

---

### `verdict completion` — Generate shell completions

```bash
verdict completion <shell>
```

| Shell | Install Command |
|-------|-----------------|
| `bash` | `verdict completion bash > /usr/local/etc/bash_completion.d/verdict` |
| `zsh` | `verdict completion zsh > ~/.zsh/completions/_verdict` |
| `fish` | `verdict completion fish > ~/.config/fish/completions/verdict.fish` |

---

### `verdict serve` — Launch FastAPI microservice

```bash
verdict serve [flags]
```

| Flag | Description |
|------|-------------|
| `--host <ip>` | Host (default: 127.0.0.1) |
| `--port <n>` | Port (default: 8000) |
| `--workers <n>` | Uvicorn workers |

---

### `verdict detect` — Detect available LLM providers

```bash
verdict detect
```

Scans for local providers (Ollama, LM Studio, etc.) and configured API keys.

---

### `verdict probe` — Run 1-token liveness probe

```bash
verdict probe <model_id>
```

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

---

### `verdict check` — Validate config syntax

```bash
verdict check [config_file]
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
| `--days <n>` | Lookback period |
| `--format <type>` | `json` \| `table` \| `csv` |

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
| `VERDICT_CONFIG` | Config file path |
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
| `3` | Config error |
| `4` | Upstream unavailable |
| `5` | No eligible models |
