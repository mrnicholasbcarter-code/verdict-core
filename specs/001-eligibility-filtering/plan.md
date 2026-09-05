# Implementation Plan: Eligibility Filtering

**Branch**: `001-eligibility-filtering` | **Date**: 2026-09-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-eligibility-filtering/spec.md`

## Summary

Protect routed work from ineligible candidates: extend the existing `EligibilityGate`
(`verdict/eligibility.py`) — already wired into `verdict/decision_kernel.py` — to (1) carry a
numeric confidence score alongside the admitted/excluded verdict, (2) make the
protected/best-available posture an explicit per-request-type flag rather than an implicit
`dev_mode` boolean, and (3) confirm/document that the single synchronous `evaluate()` call inside
`decide()` already satisfies the "re-check immediately before final selection" requirement,
since admission and final choice happen in one atomic call.

## Technical Context

**Language/Version**: Python >=3.10 (pyproject.toml `requires-python`)

**Primary Dependencies**: none new — feature extends `verdict/eligibility.py` and
`verdict/decision_kernel.py` in place; no new third-party dependency required.

**Storage**: N/A — eligibility state is derived per-call from `AvailabilityReport` /
`ModelInfo`, not persisted.

**Testing**: pytest (`tests/test_eligibility_gate.py`, `tests/test_passport_eligibility.py`),
ruff, mypy --strict, per `CLAUDE.md` baseline commands.

**Target Platform**: Linux server (verdict-core control plane, in-process library + CLI/API).

**Project Type**: Single project (Python library/control-plane, existing `verdict/` package).

**Performance Goals**: No new latency budget stated by the spec beyond existing decision-call
performance; re-check is folded into the existing single-pass `evaluate()` call so it adds no
extra round trip.

**Constraints**: Must not weaken existing fail-closed behavior for protected requests
(`verdict/eligibility.py:192-222`); must remain backward compatible with current `EligibilityRecord`/
`EligibilityResult` consumers (`decision_kernel.py`, dashboard/CLI callers of `EligibilityGate`).

**Scale/Scope**: Extends 1 existing module (`eligibility.py`) + call sites in
`decision_kernel.py`; no new services or storage.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` defines no eligibility/routing-specific gates beyond the
project's general principles (determinism, auditable decisions, no bypass of hard eligibility
checks by advisory inputs — see `verdict-core/CLAUDE.md` "Role" section). This feature is a
pure extension of the existing hard-eligibility gate, does not introduce any advisory-input
bypass, and keeps the fail-closed default. **PASS** — no violations, no Complexity Tracking
entries needed.

## Project Structure

### Documentation (this feature)

```text
specs/001-eligibility-filtering/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md         # Phase 1 output
├── quickstart.md         # Phase 1 output
└── tasks.md              # Phase 2 output (/speckit-tasks - not created here)
```

(No `contracts/` directory: eligibility filtering is an internal module contract inside
`verdict/`, not an external API/CLI surface — the observable contract is the
`EligibilityRecord`/`EligibilityResult` dataclasses already defined in `eligibility.py`, which
`data-model.md` documents instead.)

### Source Code (repository root)

```text
verdict/
├── eligibility.py        # EligibilityGate, EligibilityRecord, EligibilityResult, EligibilityVerdict (extended)
├── decision_kernel.py     # decide(): calls gate.evaluate(), consumes EligibilityResult (extended)

tests/
├── test_eligibility_gate.py       # existing + new unit tests
└── test_passport_eligibility.py   # existing + new unit tests
```

**Structure Decision**: Single project (Option 1). This feature has no new top-level
directories — it extends two existing modules and their existing test files.

## Complexity Tracking

*No violations — table intentionally omitted.*
