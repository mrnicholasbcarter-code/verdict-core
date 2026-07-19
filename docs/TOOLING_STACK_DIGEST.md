# Tooling & Framework Stack Digest — llm-gate portfolio

> Captured 2026-07-19. Purpose: preserve the authoritative contract/capabilities of every
> tool/framework in our stack so future sessions don't re-derive it. NOT verbatim docs —
> distilled with the important caveats + future-relevant pieces kept. Source paths recorded
> so the full doc can be re-read.

## 0. Topology (who does what)

```
Hermes (this CLI agent) = orchestrator. Drives atomic loop, calls tools/CLIs directly.
  └─ ruflo (swarm/agent orchestration) → shells out to agentic-flow workers
       └─ workers route model calls through OmniRoute (:20128) via OMNI_API_KEY
codex = interactive local assistant UI (OpenAI Codex CLI) + Hindsight continuation bank.
         NOT a background worker harness by itself.
llm-gate = deterministic eligibility/availability authority (the thing that makes
           "which of 3628 OmniRoute models actually work" answerable).
Hindsight = long-term memory + DOCUMENT STORE + reflect/mental-models/entities-graph.
ruvector = advisory vector retrieval / semantic memory (NOT the determinant authority).
metaharness = ruflo plugin wrapping @metaharness/darwin for quality gates
              (harness-score, mcp-scan, threat-model, oia-audit) — optional, degrades.
code-review-graph = Tree-sitter code graph + MCP; review/blast-radius/risk tooling.
```

## 1. OmniRoute (diegosouzapw/OmniRoute) — AI gateway / model router

- Local install `omniroute@3.8.48`, doc version 3.8.40. Repo: github.com/diegosouzapw/OmniRoute
  (full docs cloned to /tmp/OmniRoute during audit; authoritative = running server + repo docs).
- Runs on `:20128`. ALL our CLIs (Claude, Codex, Hermes, OpenCode, Cline, ...) point at
  `http://localhost:20128/v1` as their OpenAI-compatible base URL.
- Auth: catalog + chat = `Authorization: Bearer <OMNI_API_KEY>` (key in ~/.zshrc).
  Management endpoints (health/rate-limits/cooldowns/analytics/admin) = SEPARATE
  `manage`-scoped key. **We only have the API key, NOT the management token.**
  → `/api/monitoring/health` is PUBLIC (no-auth) and returns full live usability.
  → `/api/resilience/model-cooldowns`, `/api/rate-limits` need management token (403 today).

### Endpoints that matter to us
- `GET /v1/models` (api key) → catalog of ALL models+combos (3628). Descriptive only
  (id, context_length, capabilities: tool_calling/reasoning/thinking/temperature). Does NOT
  tell you live health. This is what llm_gate currently consumes.
- `GET /api/monitoring/health` (NO auth) → THE usability signal. Keys:
  `providerHealth`, `providerBreakers`, `circuitBreakers`, `lockouts`, `learnedLimits`,
  `quotaMonitor`, `rateLimitStatus`, `activeConnections`, `credentialHealth`,
  `providerSummary` (catalogCount/configuredCount/activeCount/monitoredCount).
- `GET /api/resilience/model-cooldowns` (mgmt) → per-(provider,connection,model) lockouts.
- `GET /api/rate-limits` (mgmt) → per-account rate-limit status.
- `GET /.well-known/agent.json` (public) → A2A agent card (v1.8.1). Skills:
  smart-routing, quota-management, provider-discovery, cost-analysis, health-report,
  list-capabilities. A2A (`POST /a2a`) is DISABLED by default (Endpoints→A2A toggle).
- `GET/POST /api/mcp/sse` and `/api/mcp/stream` → MCP server, 104 tools
  (omniroute_get_health, omniroute_list_models_catalog, route_request, etc.).
  LOCAL_ONLY tier: needs `manage`-scoped key for non-loopback; loopback may still 403
  without manage scope. We hit 403 — our API key lacks `manage`.

### Key facts / gotchas
- OmniRoute is FREE gateway: 265 providers, 90+ free, auto-fallback, token compression.
- Two model-id styles: provider-prefixed (`anthropic/claude-...`) and combo names.
- Chat completions return cost telemetry headers (X-OmniRoute-Response-Cost, etc.).
- The availability adapter in llm_gate (omniroute.py) already reads /v1/models (api key)
  + /api/monitoring/health (mgmt token). Since no mgmt token, only catalog works today;
  health MUST be switched to the no-auth /api/monitoring/health path (or we add the token).
- A2A `health-report` skill aggregates circuit-breaker/cooldown/lockout per provider —
  an alternative usability source if we enable A2A + authenticate.

## 2. llm-gate (mrnicholasbcarter-code/llm-gate) — availability/eligibility authority

- Repo at /home/nick/dev/llm-gate. main green: 313 pytest, ruff clean, mypy --strict clean.
- #56 MERGED (PR #71): `AvailabilityCache` + `GET /v1/route/explain` (OmniRouteHTTPTransport).
- #57 NEXT (not started): eligibility filtering BEFORE ranking in every route path.
  Children: #72 (wire AvailabilityCache into router/dispatcher eligibility gate),
  #73 (extend /v1/route/explain with pre-ranking eligible set + exclusions).
- Key modules: `availability_cache.py` (cache wrapping Callable[[],AvailabilityReport]),
  `api.py` (FastAPI, singleton cache), `router.py` (select_best_model — filters by catalog
  availability_state THEN ranks; does NOT consult live AvailabilityCache),
  `intelligence.py` (route() → fetch_models → select_best_model),
  `dispatcher.py`, `gate.py`, `proxy.py` (dumb pass-through), `models.py` (RoutingDecision/
  ModelInfo), `omniroute.py` (transport — proven auth contract above).
- Convention: atomic loop per slice → worktree → focused+full tests → independent review →
  fix → atomic commit → ff-merge → push → exact-SHA CI watch → update issue → retain Hindsight.
  Fail-closed: never dispatch to unproven workers; 503 if OmniRoute unset.

## 3. ruflo (agentic-flow / @claude-flow/cli) — swarm/agent orchestration

- Two installs: `npx ruflo` = v3.32.8 (used), global `ruflo` bin = v3.19.0 (STALE — ignore).
  agentic-flow = v2.0.13.
- `npx ruflo init` was run in llm-gate → wrote `.claude/`, `.claude-flow/config.yaml`,
  `.claude/settings.json` (grants `mcp__claude-flow__*`), `skills-lock.json`, `CLAUDE.md`.
- Config: swarm mesh, maxAgents 5, consensus coordination, hooks autoExecute.
- Real docs are at `llm-gate/.agents/skills/ruflo/plugins/*/README.md` (30+ plugin READMEs)
  and `llm-gate/.agents/skills/ruflo/AGENTS.md`. The npm `@metaharness` dir is an EMPTY STUB
  — ignore it; real metaharness skill wraps `npx metaharness` at runtime (degrades gracefully).
- SPARC methodology (`.agents/skills/ruflo/plugins/ruflo-sparc/.../sparc-orchestrator.md`):
  5 phases (Spec→Pseudocode→Architecture→Refinement→Completion) with gate checks;
  stores artifacts in memory namespaces `sparc-phases`/`sparc-gates`/`sparc-state`/`patterns`;
  neural learning via `neural_train`/`neural_predict`. This is the source of the
  "use vectors or insights or something to reflect" directive — it's a RUFLO skill instruction,
  not a Hindsight feature.
- Important plugin READMEs (paths under .agents/skills/ruflo/plugins/):
  - ruflo-metaharness: 11 skills (harness-score, harness-mcp-scan, harness-threat-model,
    harness-oia-audit, harness-evolve, ...). ADR-150: removable, optional, graceful degrade.
  - ruflo-ruvector: wraps ruvector@0.2.25 (PIN THIS). 91 MCP tools. Known bugs documented.
  - ruflo-intelligence: 29 tools, 4-step pipeline RETRIEVE→JUDGE→DISTILL→CONSOLIDATE.
    Namespace gotcha: `pattern` (singular, ReasoningBank) vs `patterns` (plural, pretrain).
  - ruflo-agentdb, ruflo-knowledge-graph, ruflo-rag-memory, ruflo-swarm, ruflo-core,
    ruflo-ddd, ruflo-cost-tracker, ruflo-observability, ruflo-loop-workers, etc.
- Metaharness skills we should use as #57 quality gates: `harness-score`, `harness-mcp-scan`
  (pure-read MCP security), `harness-threat-model`, `harness-oia-audit`. Requires
  `npx metaharness` resolvable (verify before relying).

## 4. ruvector (ruvector@0.2.33 installed; plugin pins 0.2.25)

- CLI at /home/nick/.npm-global/bin/ruvector. Native Rust HNSW: ~0.045ms search, 52k inserts/s.
- ADVISORY only (not the determinant for routing). Useful for: code-graph clustering
  (`hooks graph-cluster`), AST/complexity (`hooks ast-analyze`), diff analysis, semantic
  search, RAG context (`hooks rag-context`), self-learning hooks (remember/recall/trajectory).
- Known limitations (0.2.25, likely same): ONNX not bundled (need ruvector-onnx-embeddings-wasm
  for `embed text`); top-level `cluster` is "Coming Soon" (use `hooks graph-cluster`);
  `brain` needs @ruvector/pi-brain; `sona` needs @ruvector/ruvllm.
- Existing DB at /home/nick/dev/ruvector.db (1.5MB) + /home/nick/dev/.ruvector/.
- For llm-gate: could embed the model catalog + our routing decisions for "similar past
  routing" recall, but catalog is better sourced live from OmniRoute. Keep advisory.

## 5. code-review-graph (tirth8205/code-review-graph) — review/blast-radius

- Installed into llm-gate `.venv` (pip). Version 2.3.7. Repo: github.com/tirth8205/code-review-graph.
- Graph built: 764 nodes, 6224 edges across 69 files (`.code-review-graph/graph.db`).
- Use as CLI directly (not MCP — don't wire into Codex TUI):
  - `detect-changes-tool` / `review-delta` → risk-scored change analysis + blast radius.
  - `review-pr`, `pre_merge_check` → PR readiness, test-gap/dead-code detection.
  - `get_review_context_tool`, `query_graph_tool` → precise context instead of dumping files.
- Invoke via `.venv/bin/code-review-graph <cmd>` from llm-gate dir.

## 6. Hindsight (api.hindsight.vectorize.io) — long-term memory + DOCUMENT STORE

- CORRECTION: Hindsight DOES have a document store. API v0.8.4. Real base path:
  `/v1/default/banks/{bank_id}/...`. My earlier claim "vector recall only, no doc store" was WRONG.
- Banks: `codex` (our project bank, token in /home/nick/.hindsight/codex.json — [REDACTED]),
  `hermes` (my hindsight_* tools). My hindsight_* tools are locked to bank "hermes";
  codex bank accessed via execute_code with token from file (never print).
- Capabilities (from OpenAPI):
  - `/documents` (CRUD) + `/documents/{id}/chunks` + `/reprocess` → the DOCUMENT STORE.
    Use THIS to preserve tool/architecture docs with full fidelity (what user asked for).
  - `/memories` (recall/list/extract/dry-run-extract) + `/memories/{id}` → vector memory.
  - `/reflect` → reflect endpoint (the "use vectors/insights to reflect" mechanism).
  - `/mental-models` → conceptual models. `/entities` + `/entities/graph` → entity graph.
  - `/directives` → stored directives. `/consolidate` + `/consolidation/recover`.
  - `/stats`, `/operations`, `/webhooks`, `/audit-logs`, `/security-events`, `/export`, `/import`,
    `/files/retain` (retain a file as a document), `/health/llm`.
- gh project WRITE scope is missing (gh auth refresh -s project hangs non-interactively) →
  GitHub issues #56/#72/#73 are NOT linked to Project 4 board. Manual link or interactive auth.

## 7. Other inventory (present, lower priority)

- Claude Code CLI 2.1.214 (points at OmniRoute via ANTHROPIC_BASE_URL=:20128).
- agentic-flow 2.0.13 (ruflo worker runtime).
- keenable + codebase-memory-mcp MCP servers (global ~/.claude.json).
- Portfolio repos under /home/nick/dev: backtest-harness, edge-mining-framework,
  llm-gate-node, trade-risk-engine, trading-cockpit-ui, hermes-plugins, mrnicholasbcarter-code.
- Hermes plugins (47+) under /home/nick/.hermes/plugins (evey-*): autonomy, delegate,
  github, scheduler, watchdog, rag, verification, cost-guard, etc.

## 8. Open questions / gaps to resolve before #57

- Q: OmniRoute management token missing → live per-model usability (cooldowns/rate-limits)
  degraded. Options: (a) user provides OMNIROUTE_MANAGEMENT_TOKEN, (b) wire cache to
  catalog-only + no-auth /api/monitoring/health, (c) leave runtime reads disabled.
  RECOMMENDED: (b) — /api/monitoring/health is public and gives provider-level usability.
- Q: ruflo `npx ruflo init` wrote repo config (.claude/, .claude-flow/) — confirm that's
  desired (it grants mcp__claude-flow__* to Claude Code workers).
- Q: verify `npx metaharness` resolves before relying on metaharness quality gates.
- Q: code-review-graph as CLI only (yes, recommended) — no MCP wiring into Codex TUI.

## 9. Source-of-truth doc paths (re-read these, not summaries)

- OmniRoute: /tmp/OmniRoute/docs/reference/API_REFERENCE.md, CLI-TOOLS.md,
  frameworks/MCP-SERVER.md, frameworks/A2A-SERVER.md, frameworks/AGENT_PROTOCOLS_GUIDE.md.
- ruflo: /home/nick/dev/llm-gate/.agents/skills/ruflo/AGENTS.md + plugins/*/README.md
  (esp ruflo-metaharness, ruflo-ruvector, ruflo-intelligence, ruflo-core).
- ruvector: /home/nick/dev/llm-gate/.agents/skills/ruflo/plugins/ruflo-ruvector/README.md.
- code-review-graph: /tmp/code-review-graph/docs/USAGE.md, COMMANDS.md.
- Hindsight: GET api.hindsight.vectorize.io/openapi.json (live).
- llm-gate: repo source (availability_cache.py, api.py, router.py, intelligence.py, omniroute.py).
