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
├── contracts.py             # Typed contracts (TaskSpec, RoutingDecision, etc.)
├── dispatcher.py            # AssignmentExplanation, Dispatcher, SwarmDispatcher
├── eligibility.py           # EligibilityGate — hard safety floors
├── gate.py                  # Gate — composes eligibility + intelligence
├── intelligence.py          # IntelligenceService — advisory ranking (cannot bypass gate)
├── metadata/                # Core metadata store — models.dev + LiteLLM (BOD-108)
├── omniroute.py             # OmniRouteHTTPTransport — inventory/execute/health only
├── planner.py               # IntakePlanner, PlanningResult
├── probes.py                # ProbeRunner, 1-token liveness checks
├── contracts/               # JSON schemas
└── schemas/                 # OpenAPI, Pydantic models
```

## Key Flows
**Route flow**: `api.py:route()` → `Gate.route()` → `EligibilityGate.filter()` → `IntelligenceService.rank()` → `Dispatcher.assign()` → `Proxy.forward()`

**Explain flow**: `api.py:route_explain()` → `AvailabilityCache.explain()` → `EligibilityGate.explain()` → returns freshness + eligibility explain record

**Metadata refresh**: `cli.py:cmd_metadata_refresh()` → `metadata.refresh_metadata()` → models.dev + LiteLLM → `~/.verdict/model-metadata.json` (OmniRoute is never metadata SoT)

## Testing
```bash
pytest -v                    # 321 tests
ruff check .                 # lint
mypy --strict verdict/       # typecheck
```

## Configuration
- Routing YAML: `~/.config/verdict/verdict.yaml` (or `$XDG_CONFIG_HOME/verdict/verdict.yaml`)
- Context hydration TOML (narrow): `.verdict/config.toml` / `~/.verdict/config.toml`
- See `docs/CONFIGURATION.md` for the supported env surface.

## OmniRoute
- Endpoint: `http://localhost:20128/v1`
- 3,318+ models, 250+ providers, 107+ free tiers
- Smart routing: `auto/best-coding`, `auto/best-reasoning`, `auto/best-fast`
