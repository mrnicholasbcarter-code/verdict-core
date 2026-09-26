# Verdict — AGENTS.md

## Codebase Knowledge Graph
This project uses codebase-memory-mcp to maintain a knowledge graph of the codebase. Prefer MCP graph tools over grep/glob/file-search for code discovery.

### Priority Order
1. `search_graph` — find functions, classes, routes, variables by pattern
2. `trace_path` — trace who calls a function / what a function calls
3. `get_code_snippet` — read specific function/class source code
4. `query_graph` — run Cypher queries for complex patterns
5. `get_architecture` — high-level project summary

### When to Fall Back to grep/glob
- Searching string literals, error messages, config values
- Searching non-code files (Dockerfiles, shell scripts, configs)
- When MCP tools return insufficient results

## Architecture
```
verdict/
├── api.py                   # FastAPI server — /v1/route, /v1/route/explain, /v1/models
├── availability.py          # AvailabilityReport, candidates, eligibility
├── availability_cache.py    # Bounded cache, TTL, SWR, explain_freshness (issue #56)
├── benchmarking.py          # Reproducible benchmarks
├── catalog.py               # Model catalog, filters
├── cli.py                   # verdict CLI — route, explain, models, policy, dashboard
├── contracts.py             # Typed contracts (TaskSpec, RoutingDecision, etc.) — a file, not a package
├── dispatcher.py            # AssignmentExplanation, SwarmDispatcher (planning-only swarm assignment; no provider I/O)
├── eligibility.py           # EligibilityGate — hard safety floors
├── gate.py                  # Gate — sync wrapper composing IntelligenceService (used by the CLI)
├── intelligence.py          # IntelligenceService — route() is the routing-decision authority; cannot bypass EligibilityGate
├── metadata/                # Core metadata store — models.dev + LiteLLM (BOD-108)
├── omniroute.py             # OmniRouteHTTPTransport — inventory/execute/health only
├── orchestration/           # Goal-to-receipt pipeline (ADR-036)
│   ├── cli.py               #   CLI entry points: orchestrate, watch, run-receipt, supervise, eligibility
│   ├── contracts.py         #   WorkGraph, NodeState, RunReceipt, typed faults
│   ├── eligibility.py       #   DISCOVERED→ENTITLED→HEALTHY→AVAILABLE→TASK_ELIGIBLE→SELECTED ladder
│   ├── executors.py         #   Worker dispatch — Prime Agent harness calls
│   ├── planner.py           #   Frontier decomposition → WorkGraph (topology selection)
│   ├── receipt.py           #   Receipt with SHA-256 event-log digest; completion verdict
│   ├── recovery.py          #   Same-node reroute, cooldown, pool-exhaustion → FAIL_CLOSED
│   ├── review.py            #   Independent OCR (open-code-review); reviewer excluded from implementers
│   ├── run.py               #   Orchestration run loop
│   ├── runtime.py           #   Run state persistence and resume
│   ├── supervisor.py        #   Controller health monitor
│   └── tui.py               #   Live terminal view
├── planner.py               # StructuredPlanner (aliased IntakePlanner), PlanResult (aliased PlanningResult)
├── probes.py                # ProbeRunner, 1-token liveness checks
├── proxy.py                 # UpstreamProxy — actual HTTP forwarding to the resolved route
├── relay.py                 # build_attempts(), retry/idempotency safety for /v1/chat/completions and /v1/responses
├── router.py                # select_best_model(), select_best_eligible_model() — deterministic candidate ranking
└── schemas/                 # JSON Schema definitions ($id fields; contracts, receipts, passports, policy, etc.)
```

## Key Flows

**Route-decision flow** (`POST /v1/route`): `api.py:route_task()` → `IntelligenceService.route()` → `EligibilityGate.evaluate()` → `select_best_eligible_model()` (`verdict/router.py`) → `RoutingDecision` returned as JSON (no upstream call)

**Proxy flow** (`POST /v1/chat/completions`, `POST /v1/responses`): `api.py:_relay_completion()` → `IntelligenceService.route()` → `EligibilityGate.evaluate()` → `select_best_eligible_model()` → `build_attempts()` (`verdict/relay.py`) → `UpstreamProxy._forward()` (`verdict/proxy.py`)

**CLI route flow**: `cli.py` → `Gate.route()` (sync wrapper) → `IntelligenceService.route()` → `EligibilityGate.evaluate()` — same authority as the API path

**Explain flow**: `api.py:route_explain()` → `AvailabilityCache.explain()` → `EligibilityGate.evaluate()` → returns freshness + eligibility explain record

**Metadata refresh**: `cli.py:cmd_metadata_refresh()` → `metadata.refresh_metadata()` → models.dev + LiteLLM → `~/.verdict/model-metadata.json` (OmniRoute is never metadata SoT)

**Orchestration flow** (ADR-036): `verdict orchestrate <goal>` → `orchestration/planner.py` frontier decomposition → `WorkGraph` (DAG) → `orchestration/eligibility.py` per-node DISCOVERED→SELECTED ladder → `orchestration/run.py` parallel workers via Prime Agent → `orchestration/recovery.py` same-node reroute on quota/rate-limit/timeout → `orchestration/review.py` independent OCR → `orchestration/receipt.py` digest-verified receipt

## Testing
```bash
pytest -v                    # 3420 tests
ruff check .                 # lint
mypy --strict verdict/       # typecheck
```

## Configuration
- Routing YAML: `~/.config/verdict/verdict.yaml` (or `$XDG_CONFIG_HOME/verdict/verdict.yaml`)
- Context hydration TOML (narrow): `.verdict/config.toml` / `~/.verdict/config.toml`
- See `docs/CONFIGURATION.md` for the supported env surface.

## OmniRoute
- Endpoint: `http://localhost:20128/v1`
- Thousands of models across dozens of providers; live count via `verdict eligibility` (7,107 ids observed 2026-09-24)
- OmniRoute is transport and inventory only — not a metadata source of truth (ADR-032)
- Smart routing: `auto/best-coding`, `auto/best-reasoning`, `auto/best-fast` (opaque refs; dropped by eligibility gate, not candidates)
