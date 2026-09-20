# CLI Reference

```
verdict [global flags] <command> [args]
```

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
| `3` | Config error |
| `4` | Upstream unavailable |
| `5` | No eligible models |
