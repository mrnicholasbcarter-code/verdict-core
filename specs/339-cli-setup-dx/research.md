# CLI Setup DX Research — Feature 339

_Research agent: core-cli-research. Updated: 2026-09-05._

---

## 1. CLI Inventory

**Entry point:** `verdict = "verdict.cli:main"` (pyproject.toml `[project.scripts]`)
**Source:** `/home/nick/dev/verdict-core/verdict/cli.py` (2228 lines)

### Subcommands (complete, from argparse registration in `main()`):

| Subcommand | Flags | Notes |
|---|---|---|
| `setup [plan]` | `--dry-run --json --non-interactive` | Interactive wizard; reads/writes `~/.config/verdict/verdict.yaml` |
| `route <task>` | `--criticality critical/high/medium/low --terse --allow-offline` | Core routing call |
| `compare <task>` | `--criticality --allow-offline` | Direct vs Verdict comparison |
| `stats` | `--log_path` | Parses `verdict-decisions.jsonl` |
| `benchmark` | `--fixture --output-json --allow-live-provider --live-provider` | |
| `quickstart` | `--json --non-interactive --dry-run` | Credential-free flagship demo |
| `ui` | (none) | Launches Streamlit dashboard |
| `serve` | `--port --host` | FastAPI microservice |
| `detect` | `--verbose/-v --json --config` | Port-probe for local providers/gateways |
| `probe <models>+` | `--base-url --timeout --allow-live-probe --json` | 1-token liveness probe |
| `catalog` | `--base-url --management --expected-rows --freshness-seconds --db-path --probe --probe-limit --probe-timeout --allow-live-probe --json` | OmniRoute catalog qualify |
| `suggest` | `--log_path` | Intelligence suggestions from telemetry |
| `doctor` | `--fix --json` | Scan/repair config and connectivity |
| `runtime status/explain/reconcile` | `--json --apply --yes --service` | Daemon ownership |
| `uninstall` | `--purge-data` | Remove memory bridge hooks |
| `check` | (none) | Validate `verdict.yaml` |
| `compat manifest/check` | `--json --declared` | Cross-repo contract gate |
| `memory put/search/export/import/masterdocs/graph/docs/setup` | various | Memory plane |
| `mcp serve/init/status` | `--json` | MCP stdio server |
| `hook recall/record/configure/status` | various | Lifecycle hooks |
| `run` | `--terse --criticality` | Alias of `route` |
| `plan` | `--json` | Mutation-free setup plan |
| `models` | `--json` | List model catalog |
| `inspect <model_id>` | `--json` | Inspect one model |
| `replay <session_id>` | `--json` | Replay recorded session |
| `simulate <task>` | `--criticality --model --json` | Pre-execution cost/risk forecast |

**Unregistered (defined but not wired in argparse):** `cmd_cost_report` — no `cost-report` subcommand exists in `main()`.

---

## 2. Config File

- **CLI path:** `${XDG_CONFIG_HOME:-$HOME/.config}/verdict/verdict.yaml`
- **Setup wizard writes:** `verdict.yaml` at that path
- **quickstart.sh writes:** `config.yaml` at same dir — **MISMATCH** (bug)
- **gate.py reads:** `verdict.yaml`
- **Format:** YAML with keys `primary_model`, `providers` (dict), `log_path`

---

## 3. Environment Variable Inventory

### User-facing (CLI + routing)

| Variable | Where read | Default | Purpose |
|---|---|---|---|
| `XDG_CONFIG_HOME` | cli.py, gate.py, setup_plan.py | `~/.config` | Config dir root |
| `OMNIROUTE_BASE_URL` | cli.py, api.py, subagent_models.py | None (required for OmniRoute features) | OmniRoute base URL |
| `OMNIROUTE_API_KEY` | cli.py, api.py | None | OmniRoute auth token |
| `OPENAI_API_KEY` | cli.py (probe transport) | None | Probe auth |
| `GEMINI_API_KEY` | cli.py (setup wizard) | None | Gemini fallback |
| `OPENROUTER_API_KEY` | cli.py (setup wizard) | None | OpenRouter fallback |
| `VERDICT_MEMORY_DB` | cli.py | `~/.verdict/memory.db` | Memory plane path |

### API server / LLM gate

| Variable | Default | Purpose |
|---|---|---|
| `LLMGATE_HOST` | `127.0.0.1` | Server bind address |
| `LLMGATE_UNIX_SOCKET` | None | Unix socket path |
| `LLMGATE_AUTH_TOKEN` | None | Gate auth token |
| `LLMGATE_ALLOW_ANONYMOUS` | `false` | Allow unauthenticated |
| `LLMGATE_UPSTREAM_BASE_URL` | `http://localhost:20128/v1` | Upstream routing target |
| `LLMGATE_UPSTREAM_API_KEY` | None (falls back to `OMNIROUTE_API_KEY`) | Upstream auth |
| `LLMGATE_UPSTREAM_TIMEOUT_MS` | `30000` | Upstream timeout |
| `LLMGATE_PRIMARY` | `anthropic/claude-3-opus-20240229` | Primary model ID |
| `LLMGATE_LOG_PATH` | `verdict-decisions.jsonl` | Decision log |
| `LLMGATE_DISCOVERY_TTL_SECONDS` | `60` | Discovery cache TTL |
| `LLMGATE_AVAILABILITY_PROFILE` | `development` | Triggers live probes when `production` |
| `LLMGATE_ALLOW_LIVE_PROBES` | `""` | Consent to network probes |
| `LLMGATE_PROBE_BASE_URL` | None | Override probe target |
| `LLMGATE_PROBE_API_KEY` | None | Probe auth |
| `LLMGATE_PROBE_CONSENTED` | `""` | Probe consent flag |
| `LLMGATE_INTELLIGENCE_PROFILE` | `development` | Intelligence gate profile |
| `LLMGATE_INTELLIGENCE_TIMEOUT_MS` | (internal default) | Intelligence timeout |
| `LLMGATE_FRONTIER_ALLOWLIST` | None | Allowed frontier models |
| `LLMGATE_MODEL_ALLOWLIST` | None | Model allowlist |
| `LLMGATE_MODEL_DENYLIST` | None | Model denylist |
| `LLMGATE_MODEL_PASSPORT_TTL` | `300` | Passport TTL |
| `LLMGATE_MAX_REQUEST_BYTES` | (DEFAULT_MAX_REQUEST_BYTES) | Max body size |
| `OMNIROUTE_MANAGEMENT_TOKEN` | None | OmniRoute management auth |
| `OMNIROUTE_USAGE_API_KEY_ID` | None | Usage tracking key ID |
| `OMNIROUTE_ALLOW_PRIVATE_HOSTS` | `""` | Allow private hosts |
| `VERDICT_RECEIPTS_DB` / `VERDICT_EVIDENCE_DB` | None | Evidence receipt store |
| `VERDICT_EVIDENCE_MAX_ENTRIES` | `256` | Max evidence entries |

### Documentation / toolchain

| Variable | Purpose |
|---|---|
| `VERDICT_MEMORY_PLANE_PATH` | Memory plane DB path (docs preflight) |
| `VERDICT_RUFLO_ROOT` | Local Ruflo checkout path |
| `VERDICT_RUFLO_REF` | Ruflo git ref |
| `VERDICT_RUVECTOR_ROOT` | Local RuVector checkout path |
| `VERDICT_RUVECTOR_REF` | RuVector git ref |
| `VERDICT_GUIDANCE_PATH` | Guidance YAML path |
| `VERDICT_GUIDANCE_LOCAL_PATH` | Local guidance override |
| `VERDICT_RUNTIME_STATE_DIR` | Runtime state directory |
| `PI_API_KEY` | Pi AI tool detection (memory bridge) |
| `HERMES_API_KEY` | Hermes tool detection (memory bridge) |

**No `.env.example` exists documenting any of these.** Only `.env.memory.example` covers the Hindsight/memory subsystem.

---

## 4. Gateway Autodetection

### What exists (`verdict/provider_detection.py`)

- Port-probes via `_check_port(port, host="127.0.0.1")` — TCP connect, no HTTP validation
- Detected routers:
  - 9router: ports 20128 or 20132; default URL `http://localhost:20128/v1`
  - omniroute: ports 20132 or 20128; default URL `http://localhost:20132/v1`
  - Detection label ↔ port assignment is **swapped** from reality

### Runtime reality (verified 2026-09-05)

- OmniRoute is running at **localhost:20128**
- Health: `GET /api/health` → `{"status":"ok"}`
- Models: `GET /v1/models` → OpenAI-compatible list of combos/models
- Settings: `GET /api/settings` → full config object including `runtimePorts.port: 20128`
- The `runtimePorts` field provides a machine-readable identity beacon

### What's missing

1. `OMNIROUTE_BASE_URL` has **no default** — even when OmniRoute is running on :20128, the CLI falls back to `return None` in `_omniroute_api_request()` if the variable is unset. Detection exists (`verdict detect`) but the result is never wired back into `setup`'s OmniRoute path.
2. No `/api/health` check before registering gateway nodes — silent failure is the current behavior.
3. `verdict probe` defaults to `http://localhost:20128/v1` but requires `--allow-live-probe` — that URL is not auto-populated from detection.
4. No structured gateway identity: the detected port tells you nothing about which gateway (9router vs omniroute). `GET /api/settings` returns `runtimePorts` which can distinguish, but this is not used.

---

## 5. Gaps Table

| # | Gap | Evidence | Proposed fix | Overlaps #237 |
|---|---|---|---|---|
| 1 | `OMNIROUTE_BASE_URL` not defaulted after detection | `_omniroute_api_request` returns None without it; `verdict detect` result not wired to setup | Auto-set `OMNIROUTE_BASE_URL` from detected gateway URL during setup; or default to first healthy gateway | No (new) |
| 2 | No `.env.example` documenting env vars | Only `.env.memory.example` exists; 35+ undocumented vars scattered across 6 files | Add `.env.example` with every variable, grouped, with defaults and notes | No (new) |
| 3 | `quickstart.sh` writes `config.yaml`; gate reads `verdict.yaml` | quickstart.sh line 28 vs gate.py line 73 | Fix quickstart.sh to write `verdict.yaml` | No (bug) |
| 4 | `cmd_cost_report` not registered in argparse | Defined cli.py:605 but missing from main() | Wire as `verdict cost-report` subcommand | No (new) |
| 5 | No `verdict init` command | Issue #237 AC, code search confirms absence | Implement `verdict init` that creates validated local config from prompts (distinct from `setup` wizard) | YES — #237 |
| 6 | No end-user copy-paste setup script (post-install) | quickstart.sh installs via pip but doesn't handle post-install config interactively | Add `verdict setup --quick` or document `verdict detect && verdict setup --non-interactive` flow | Partial (#237 mentions quickstart scripts) |
| 7 | Port confusion in detection labels | provider_detection.py:83-99 swaps 9router/omniroute default ports | Fix to match reality: OmniRoute primary at :20128, 9router at :20129 | No (bug) |
| 8 | No dev-mode workflow documented | No `verdict serve --dev`, no `scripts/dev-start.sh` | Add dev quickstart path: `uv sync --extra dev --extra server && verdict serve` | No (new) |

---

## 6. Overlap with Issue #237

#237 (`[DX-001] CLI explain, init, local defaults, and integration harness`) covers:
- `verdict init` — creates validated local config (NOT the same as `verdict setup`) **[needs implementing]**
- `verdict explain` / dry-run / evidence commands
- Deterministic local providers / credential-free quickstart
- Integration harness end-to-end verification
- No unauthorized network calls (offline-first)

**This feature (339) scope: END-USER SETUP DX (not contributor)**
- Does NOT duplicate `verdict init` — references #237 as dependency
- Adds: env var documentation, gateway autodetect wiring, copy-paste install script, `OMNIROUTE_BASE_URL` default logic, bug fixes (config filename, cost-report, port labels)

---

## 7. Proposed End-User Setup Script (DRAFT — not installed)

```bash
#!/usr/bin/env bash
# verdict-setup.sh — End-user Verdict setup (copy-paste, no curl-pipe-bash)
# Requires: Python 3.10+, pip or pipx
set -euo pipefail

# 1. Install
if command -v pipx &>/dev/null; then
  pipx install "verdict-core"
else
  pip install --user "verdict-core"
fi

# 2. Detect gateways and providers
echo "Scanning for local providers..."
verdict detect --json > /tmp/verdict-detected.json 2>/dev/null || true

# 3. Auto-wire OMNIROUTE_BASE_URL if OmniRoute is running
if curl -sf http://localhost:20128/api/health >/dev/null 2>&1; then
  export OMNIROUTE_BASE_URL="http://localhost:20128"
  echo "OmniRoute detected at localhost:20128"
elif curl -sf http://localhost:20129/api/health >/dev/null 2>&1; then
  export OMNIROUTE_BASE_URL="http://localhost:20129"
  echo "9router detected at localhost:20129"
fi

# 4. Run interactive setup (uses detection results automatically)
verdict setup

# 5. Verify
verdict check && verdict quickstart
echo "Verdict is ready. Try: verdict route 'your task here' --criticality medium"
```

_This draft does not handle: pipx path, Windows, API keys for cloud providers, or multi-gateway scenarios. Refine in implementation._

---

## 8. Speckit Artifacts

- Feature dir: `specs/339-cli-setup-dx/`
- This file: `research.md`
- Spec: (to be generated by speckit-specify)
- Plan: (to be generated by speckit-plan)
