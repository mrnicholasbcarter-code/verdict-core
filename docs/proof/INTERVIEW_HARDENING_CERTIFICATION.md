# Interview hardening certification & cleanup report (BOD-178 → BOD-183)

> **Superseded:** this was a self-reported certification for PR #589. The authoritative, fresh-clone certification with live evidence is [INTERVIEW_GOLDEN_PATH_CERTIFICATION.md](INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).

**Date:** 2026-09-24  
**Branch:** `cursor/interview-hardening-bod-178-2d40`  
**Linear:** [BOD-178](https://linear.app/bodanglin/issue/BOD-178) epic;
stories BOD-179 … BOD-183.

## Intent

Leave `verdict-core` with a truthful, interviewable repository surface:
lifecycle-classified ADRs, shipped-behavior docs, evidence-backed deletions,
supported config/env only, and a fresh-clone certification matrix.

## What changed

### BOD-179 — ADR inventory

| Action | Item |
|---|---|
| Consumes | [BOD-169](https://linear.app/bodanglin/issue/BOD-169) / [`verdict-ecosystem` ADR_LIFECYCLE](https://github.com/mrnicholasbcarter-code/verdict-ecosystem/blob/main/docs/ADR_LIFECYCLE.md) as **cross-repo evidence authority** (0 CURRENT by design) |
| Rewrote | `docs/adr/README.md` — navigable in-repo index aligned to BOD-169 classifications (PARTIALLY_TRUE / DUPLICATE / SUPERSEDED / …) |
| Indexed | Previously missing `0001`, `ADR-033`, `ADR-034` |
| Added | `ADR-035` superseding ADR-023 — closes BOD-169 `MISSING_SUCCESSOR` |
| Marked DUPLICATE | `docs/architecture/ADR-EVIDENCE-LEDGER.md`, `docs/architecture/ADR-ORCHESTRATOR-ROUTING.md` |
| Recorded gaps | Session economics, capability/budget governors, CURRENT promotion pending ecosystem re-audit |

### BOD-180 — Documentation consolidation

| Action | Item |
|---|---|
| Rewrote | `docs/GETTING_STARTED.md` — correct org URL, YAML config, no Opus-as-fallback example |
| Rewrote | `docs/CONFIGURATION.md` — YAML SoT + real env keys; removed fictional TOML schema |
| Rewrote | `docs/architecture.md` — Gate.route / EligibilityGate.evaluate; OmniRoute inventory-only; no invented WS/RTK/perf SLA |
| Updated | `README.md` docs table + ADR count (through 034) |
| Fixed | `docs/CLI_REFERENCE.md` exit codes (1/2/3 only) |
| Sanitized | `docs/guides/prime-workflow.md` machine-local `/home/nick/...` paths |
| Updated | `.hermes.md` Spec Kit pointer after `.specify/feature.json` removal |

### BOD-181 — Dead code / artifact cleanup

Each deletion had zero production importers (or was an explicitly non-authoritative script / tracked session artifact):

| Removed | Evidence |
|---|---|
| `verdict/git_hooks.py` | No imports/CLI/tests; superseded by `.pre-commit-config.yaml` |
| `verdict/litellm_adapter.py` + `tests/test_litellm_adapter.py` | Tests-only; live LiteLLM path is `verdict/metadata/sources.py` |
| `verdict/adaptive_state.py` + `tests/test_adaptive_state.py` | Tests-only; never imported by ranker/CLI |
| `scripts/ci_watch.sh` | No CI/docs refs |
| `scripts/terminal_preview.py` | No refs |
| `scripts/demo-routing.py` | Non-authoritative; replaced by `verdict/routing_demo.py` |
| `universal-integration.sh` | Zero repo references |
| `.codex/AGENTS.override.md` | Tracked local override template |
| `.specify/feature.json` | Stale Spec Kit pointer to completed `specs/455-…` |
| `.verdict/BOD-*-PLAN.md`, `bod-58-assessment.md`, `handoff.md` | Tracked session artifacts |

**Retained intentionally (BOD-127 residuals):** `chooser.py`, `live_routing.py`,
`failover_engine.py` — demoted feeds/escapes, not deleted. Documented as
non-authority when serve-path EP is required.

### BOD-182 — Config / env / harness surface

| Action | Item |
|---|---|
| Documented | YAML routing path vs context TOML path in CONFIGURATION + GETTING_STARTED |
| Archived | `.env.memory.example` Hindsight vars → inert placeholders pointing at `docs/archive/PROJECT_MEMORY.md` |
| Ignored | `.verdict/*PLAN*`, handoff, assessments in `.gitignore` |
| Audited | `.env.example` remains the commented inventory of live keys |

### BOD-183 — Certification matrix

Run from this branch after install:

```bash
# Fresh contributor install
uv sync --extra dev

# Credential-free flagship proof
uv run python -m verdict quickstart --non-interactive --dry-run

# Unit/integration + quality gates used by the project
uv run pytest -q
uv run ruff check .
uv run mypy --strict verdict/

# Packaging / CLI smoke
uv run python -c "import verdict; print(verdict.__name__)"
uv run python -m verdict --help

# Offline failover proof (no network)
uv run python -m verdict failover-proof --memory-path /tmp/verdict-failover.db --json

# Docs / ADR link sanity (manual spot-check)
test -f docs/adr/README.md
test -f docs/architecture.md
test -f docs/GETTING_STARTED.md
test -f docs/CONFIGURATION.md

# Clean tree after normal operation
git status --porcelain
```

**Recorded results (this agent run, 2026-09-24):**

| Check | Result |
|---|---|
| `uv sync --extra dev --extra server` | PASS |
| `uv run python -m verdict quickstart --non-interactive --dry-run` | PASS (`demo/frontier-tools`, 3 named drops) |
| `uv run python -c "import verdict"` | PASS |
| `uv run python -m verdict --help` | PASS |
| `uv run ruff check verdict` | PASS |
| `uv run mypy --strict verdict/` | PASS (188 source files) |
| `uv run pytest -q` | PASS — **2581 passed**, 3 skipped |
| `uv run python -m verdict failover-proof … --json` | PASS (replay digest emitted) |
| `git status --porcelain` after gates | Only intentional hardening diffs |

1. README — product purpose, fail-closed pitch, quickstart receipt
2. `docs/architecture.md` — Gate → Eligibility → Intelligence → Serve path → Proxy
3. `docs/adr/README.md` — CURRENT vs SUPERSEDED decisions + evidence
4. `docs/proof/EVIDENCE_INDEX.md` — what claims are proven

## Explicitly not done in this pass

- Full CLI_REFERENCE regeneration of every subcommand section (exit codes + config path corrected; command table already in README).
- Deleting demoted selector modules (`chooser` / `FailoverEngine` invent paths) — requires follow-up invariant work beyond BOD-127.
- BOD-177 Verdict-native TUI — separate epic; docs acknowledge it as planned presentation work.
- Portfolio public `/docs` surface — BOD-184 in `nickcarter-dev` (parallel track).

## Residual risks

- Some `docs/architecture/*_SPECIFICATION.md` and `docs/specs/*` remain design-era prose; they are labeled non-current via architecture.md and the ADR index rather than mass-deleted.
- Declared Status fields inside individual ADR files still use older vocabulary; the index Lifecycle column is authoritative until a follow-up rewrites each file header.
