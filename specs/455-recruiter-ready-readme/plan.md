# Implementation Plan: Recruiter-Ready README and Proof Demo

**Branch**: `feat/455-recruiter-ready-readme` | **Date**: 2026-09-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/455-recruiter-ready-readme/spec.md`

## Summary

Make the public proof path mechanically match the credential-free CLI output, preserve the existing deterministic fixture and public CLI boundary, and collect prospective closure evidence required by issue #455. The implementation should prefer documentation and regression-test changes; runtime behavior changes are permitted only if an exact-output contract cannot otherwise be satisfied.

## Technical Context

**Language/Version**: Python 3.10+ and Bash

**Primary Dependencies**: Existing Verdict CLI and deterministic flagship fixture; Python standard library; pytest and ruff already declared by the repository

**Storage**: Repository files only for prospective public evidence; the proof command must not write application state

**Testing**: pytest, ruff, shell syntax validation, repository CI/check runs, and explicit README relative-link plus fragment validation

**Target Platform**: Installed Python CLI on Linux/macOS; credential-free deterministic path must also run from an empty working directory

**Project Type**: Python library and CLI with Markdown documentation and a shell installer

**Performance Goals**: No new performance budget; the user-facing proof must remain suitable for a 90-second evaluation journey

**Constraints**: No provider credentials, gateway, provider call, invented transcript, invented timing, or retrospective evidence; preserve public behavior outside the already-specified fixture receipt rendering; keep claims within the existing proof boundary

**Scale/Scope**: One README journey, one deterministic fixture/report, one installer verification command, focused regression coverage, and issue #455 evidence

## Constitution Check

*GATE: Passed before research and re-checked after design.*

- **Repository ownership**: PASS — all planned files belong to `verdict-core`; the user explicitly approved `specs/455-recruiter-ready-readme/` for this story.
- **Documentation before dependencies**: PASS — no dependency is added or changed.
- **Verification is part of the change**: PASS — tasks bind requirements to focused tests, clean-environment execution, link/fragment checks, terminal capture, and exact-head CI evidence.
- **Evidence honesty**: PASS — prospective commands must capture actual output and failure status; automated checks are not represented as human skim verification.
- **Least authority**: PASS — no credentials, provider calls, publishing, merge, or unrelated changes are planned.
- **Single ownership**: PASS — implementation is reserved for the coordinator after these records are complete.

Post-design re-check: PASS. The data model and CLI contract are descriptive, introduce no service or persistence layer, and keep missing human or remote evidence explicitly blocked.

## Project Structure

### Documentation (this feature)

```text
specs/455-recruiter-ready-readme/
├── spec.md
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   └── proof-output.md
├── checklists/
│   └── requirements.md
└── tasks.md
```

### Source Code (repository root)

```text
README.md
quickstart.sh
verdict/flagship_demo.py
tests/test_flagship_demo.py
tests/test_documentation_smoke.py
docs/proof/                    # prospective evidence location if repository convention confirms it
.github/workflows/              # read-only source of required check names
```

**Structure Decision**: Reuse the existing CLI fixture and documentation-smoke tests. Add only the smallest test/evidence artifacts needed to make README output and links verifiable; do not introduce a second renderer or routing path.

## Phase 0: Research Outcome

See [research.md](research.md). No `NEEDS CLARIFICATION` items remain. The decisive choices are exact-output comparison, a repository-native link/fragment audit, an actual terminal recording generated from the final command, and separate recording of automated versus human evidence.

## Phase 1: Design Outcome

- [data-model.md](data-model.md) defines the proof block, fixture receipt metadata, link target, and evidence-record states.
- [contracts/proof-output.md](contracts/proof-output.md) defines the CLI/README exactness boundary.
- [quickstart.md](quickstart.md) defines prospective validation without asserting results that have not yet been observed.

## Complexity Tracking

No constitution violation or additional abstraction is planned.
