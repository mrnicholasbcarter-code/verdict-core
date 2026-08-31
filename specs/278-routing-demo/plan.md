# Implementation Plan: Routing Demo Cost vs Quality

**Branch**: `feat/278-routing-demo` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/278-routing-demo/spec.md`

## Summary

Deliver a portfolio-readable demo that routes exactly 100 heterogeneous requests with the existing live-routing cheaper-first policy, prices routed vs costliest-qualified baseline from a live (or labeled recorded) catalog/pricing capture, runs bounded real executes for latency/success evidence, and prints per-request rationales plus aggregate savings — without inventing fixture catalogs as the proof. Live catalog unreachable → blocked.

## Technical Context

**Language/Version**: Python 3.10+ (repo `requires-python`), typed modules under `verdict/`

**Primary Dependencies**: Existing `verdict.live_routing` (classify/select/explain/cheaper-first), `verdict.live_routing_gateway` (fetch_models, pricing index, probe/execute), `httpx`, pytest

**Storage**: Optional JSON evidence under `docs/benchmarks/` or `evidence/` for labeled recorded captures; no new database

**Testing**: pytest; ruff; mypy `--strict` on touched modules

**Target Platform**: Linux local operator machine with OmniRoute-compatible gateway (`http://localhost:20128/v1`)

**Project Type**: library + runnable demo module / docs

**Performance Goals**: Full demo wall clock < 60 seconds when live catalog is healthy

**Constraints**: Do not rewrite cheaper-first; do not invent fixture prices as demo proof; do not implement 272 P3 / ADK; do not fight `context_pack.py`; secrets never in output

**Scale/Scope**: Exactly 100 requests; one gateway; one summary artifact

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status |
|---|---|
| I. Coordination ≠ delivery | PASS — demo run + evidence are the proof |
| II. Docs before deps | PASS — reuse live_routing + OmniRoute contracts already researched |
| III. Repo boundaries | PASS — only this worktree; specs under `specs/278-routing-demo/` |
| IV. Verification | PASS — pytest + live/recorded evidence; blocked ≠ pass |
| V. Least authority | PASS — no credential writes; strip secrets |

Post-design: still PASS.

## Project Structure

### Documentation (this feature)

```text
specs/278-routing-demo/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── routing-demo.md
├── checklists/
└── tasks.md                 # created by /speckit-tasks, not this command
```

### Source Code (repository root)

```text
verdict/
├── live_routing.py              # reuse cheaper-first (do not rewrite)
├── live_routing_gateway.py      # reuse fetch/pricing/probe helpers
├── live_routing_run.py          # reuse orchestrator pieces as needed
└── routing_demo.py              # NEW: 100-request demo + baseline/savings

docs/benchmarks/
└── routing-demo.md              # NEW: how to run / interpret

tests/
├── test_routing_demo.py         # NEW: unit/rule tests (may use recorded capture fixtures labeled as non-live)
└── test_routing_demo_live.py    # NEW: live gateway tests; blocked when unreachable

examples/routing-demo/           # OPTIONAL thin wrapper for issue-path discoverability
└── README.md / run.py
```

**Structure Decision**: Single-project Python package. New demo module `verdict/routing_demo.py` owns the 100-request loop, baseline math, and summary formatting. Reuse Feature 276 live-routing policy and gateway fetch; do not duplicate selection policy. Docs live in `docs/benchmarks/routing-demo.md`.

## Complexity Tracking

> No constitution violations requiring justification.
