---
description: "Task list for routing demo cost vs quality"
---

# Tasks: Routing Demo Cost vs Quality

**Input**: [plan.md](./plan.md), [spec.md](./spec.md), [data-model.md](./data-model.md), [contracts/routing-demo.md](./contracts/routing-demo.md)

**Tests**: Required (portfolio honesty + cheaper-first).

## Phase 1 — Setup

- [x] T001 Create `verdict/routing_demo.py` module skeleton with `SCHEMA_VERSION = "routing-demo/v1"` and package `__main__` entry in `verdict/routing_demo.py`
- [x] T002 [P] Add `docs/benchmarks/routing-demo.md` mirroring [quickstart.md](./quickstart.md) for reviewers
- [x] T003 [P] Add optional thin `examples/routing-demo/README.md` pointing at `python -m verdict.routing_demo` for issue-path discoverability

## Phase 2 — Foundational

- [x] T004 Expose or wrap live pricing index fetch for demo use without rewriting cheaper-first in `verdict/live_routing_gateway.py` (export helper) and `verdict/routing_demo.py`
- [x] T005 Implement deterministic 100-request mix builder (`simple`/`complex`) in `verdict/routing_demo.py`
- [x] T006 Implement USD estimator from live/recorded pricing quotes in `verdict/routing_demo.py`
- [x] T007 [P] Write failing unit tests for estimator, baseline=costliest, cheaper-first reuse, blocked shape in `tests/test_routing_demo.py`

## Phase 3 — User Story 1 (Portfolio demo output) [US1]

**Goal**: Run 100 requests; show per-request decisions + aggregate savings + quality metrics in <60s when catalog healthy.

**Independent Test**: Live or recorded run produces 100 decisions, savings block, latency/success, wall clock metric.

- [x] T008 [US1] Implement catalog load (live fetch or labeled recorded) + classify via `verdict.live_routing.classify_identities` in `verdict/routing_demo.py`
- [x] T009 [US1] Implement per-request qualify → `select_route` chosen + costliest baseline + rationale records in `verdict/routing_demo.py`
- [x] T010 [US1] Aggregate summary (`routed_cost_usd`, `baseline_cost_usd`, `savings_*`, wall clock) and human-readable + JSON printers in `verdict/routing_demo.py`
- [x] T011 [US1] Wire CLI (`--gateway`, `--recorded`, `--json`) and non-zero exit on blocked in `verdict/routing_demo.py`
- [x] T012 [P] [US1] Extend `tests/test_routing_demo.py` for 100 decisions and summary contract fields

## Phase 4 — User Story 2 (Cheaper-first spend) [US2]

**Goal**: Paid never chosen while cheaper qualified kept remains; rationales say so.

**Independent Test**: Mixed catalog capture asserts SC-005.

- [x] T013 [US2] Ensure request-class filters never bypass `select_route` cheaper-first; add assertion helpers in `verdict/routing_demo.py`
- [x] T014 [P] [US2] Tests proving paid-not-selected-when-cheaper-kept using labeled recorded/minimal real-shaped capture in `tests/test_routing_demo.py`

## Phase 5 — User Story 3 (Blocked / recorded honesty) [US3]

**Goal**: Unreachable live → blocked; recorded explicitly labeled; no secret leakage.

**Independent Test**: Bad gateway → blocked JSON; recorded path → `mode=recorded`.

- [x] T015 [US3] Map `LiveSurfaceBlocked` to `status=blocked` without fixture fallback in `verdict/routing_demo.py`
- [x] T016 [US3] Implement recorded capture load/save helpers with mandatory mode labeling in `verdict/routing_demo.py`
- [x] T017 [US3] Bounded execute sampling for latency/success (unique chosen ids, short max_tokens) with honest failures in `verdict/routing_demo.py`
- [x] T018 [P] [US3] Live test module `tests/test_routing_demo_live.py` (catalog reachable completes; unreachable blocked ≠ pass)
- [x] T019 [P] [US3] Secret-stripping / no-prompt assertions in `tests/test_routing_demo.py`

## Phase 6 — Polish

- [x] T020 Run focused pytest + `ruff check` + `mypy --strict` on touched modules
- [x] T021 Capture live or recorded evidence under `docs/benchmarks/` when OmniRoute allows; if catalog down report blocked in converge notes
- [x] T022 Confirm no edits to Spec 272 Phase 3 / `context_pack.py` / ADK surfaces
- [x] T023 Carry absorbed #282 criteria into the supported entrypoint: deterministic `--mock` Opus/Sonnet/Haiku/auto cost comparison, terminal + JSON output, under 30 seconds, eligibility-gated adaptive-ranker evidence, no provider spend
- [x] T024 Reject non-chat modalities and require observed tools capability for complex live requests; exact named chat checks must pass before live savings are claimed

## Dependency graph

```text
T001
 ├─ T002, T003 [P]
 └─ T004 → T005 → T006 → T007
      └─ T008 → T009 → T010 → T011 → T012
           └─ T013 → T014
                └─ T015 → T016 → T017 → T018/T019
                     └─ T020 → T021 → T022
```

## MVP

T001–T012: readable 100-request savings demo on live catalog. T013–T019 close honesty gates. T020–T022 polish.

## Parallel examples

- After T001: T002 || T003
- After T006: T007 in parallel with early T008 scaffolding
- After T017: T018 || T019
