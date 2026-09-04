# Implementation Plan: Cross-Repository Security and Privacy Launch Gate

**Branch**: `feat/238-launch-001` | **Date**: 2026-09-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/277-security-privacy-gate/spec.md`

## Summary

Extend verdict-core's existing non-advisory security CI gates (pip-audit, bandit,
npm audit, osv-scanner, CodeQL — delivered under issue #271 / spec 271) with three
new blocking release-pipeline stages — SBOM generation, build provenance
attestation, and a dynamic (runtime) security check against the built artifact
running in an isolated local/sandboxed container in CI — plus a blocking
PII/secret boundary test suite for every memory/learning subsystem, and
documented + automatically-verified data retention/erasure (30-day,
GDPR-equivalent) and telemetry-consent behavior. All new checks join the existing
gates as required, non-skippable release-blocking evidence; any critical/high
finding blocks release unless resolved via an explicit, attributed, recorded
reviewer waiver (or, for a full gate-infrastructure outage, a named emergency
approver's attributed waiver).

## Technical Context

**Language/Version**: Python 3.10+ (CI matrix tests 3.10–3.13; security/CodeQL
jobs pin 3.12); Node 20 for the `node-security` npm-audit job (client/contracts
packages under this repo's `package-lock.json`)

**Primary Dependencies**: uv (dependency/build management), pytest +
pytest-asyncio + pytest-cov (testing), ruff (lint/format), mypy --strict (typing),
pip-audit + bandit (existing Python security gates), npm audit + osv-scanner
(existing dependency gates), CodeQL (existing SAST); new: an SBOM generator
(format TBD in research — CycloneDX or SPDX), a provenance/attestation mechanism
(TBD — SLSA/in-toto style), a dynamic/DAST-style scanner runnable against a local
container (TBD)

**Storage**: N/A for this feature's own state — the Launch Gate Evidence Record
is CI-run-scoped output (artifacts + a machine-readable evidence file per run),
not a new persistent datastore. Retention/erasure tests exercise existing
memory/learning storage (see `verdict/memory_*.py` adapters) rather than
introducing new storage.

**Testing**: pytest (existing suite under `tests/`); new PII/secret boundary
tests and retention/erasure/telemetry-consent tests join the existing
non-advisory `uv run pytest -q` baseline and the CI-blocking security jobs

**Target Platform**: Linux CI runners (GitHub Actions, `ubuntu-latest`), matching
the existing `security.yml` / `codeql.yml` / `ci.yml` workflows

**Project Type**: Single Python library/control-plane project with CI/CD
pipeline extensions (`.github/workflows/`) — no new frontend or mobile surface

**Performance Goals**: Not applicable as a runtime performance target; the
operational goal is pipeline evidence completeness (SC-001, SC-002, SC-004,
SC-005) and reviewer reproducibility time (SC-003: full evidence set
reproducible from a clean checkout in under 30 minutes)

**Constraints**: New gates MUST be non-advisory (blocking), matching the
existing `python-security` / `node-security` / `osv-security` job behavior; the
dynamic check MUST run against a local/sandboxed container or process within CI
only (no staging deployment); an unavailable/degraded check MUST NOT be
interpreted as a pass (FR-009); waivers MUST be attributed and recorded, never
silent (FR-010, FR-011)

**Scale/Scope**: Scoped to verdict-core's own release pipeline and its
documented in-repo dependencies (VER-008, VER-011, MEM-001, REL-001 per spec.md
Assumptions); cross-repository evidence aggregation with verdict-node and
verdict-ecosystem depends on REL-001 landing separately and is out of scope here

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- **I. Coordination Is Governance, Execution Is Delivery** — PASS. This plan is
  grounded in direct inspection of `pyproject.toml`, `.github/workflows/security.yml`,
  and `.github/workflows/codeql.yml` (read in full), not secondhand summaries.
  No coordination record is treated as evidence of completion.
- **II. Documentation Before Dependencies** — PASS (pending Phase 0). SBOM
  format, provenance mechanism, and dynamic-scan tooling are explicitly deferred
  to Phase 0 research rather than assumed; each will be selected only after
  reading its own docs/schema, per Assumptions in spec.md.
- **III. Repository Boundaries Are Non-Negotiable** — PASS. Scope is explicitly
  bounded to verdict-core's own pipeline (spec.md Assumptions). Cross-repo
  evidence aggregation is named as a REL-001 dependency, not pulled into this
  feature's implementation.
- **IV. Verification Is Part of the Change** — PASS (design intent). FR-004 and
  FR-009 require the new checks to be non-advisory and to treat
  unavailable/degraded results as failures, matching this repo's existing
  blocking-gate convention rather than inventing a parallel advisory-only path.
- **V. Safety, Reversibility, and Least Authority** — PASS (design intent).
  FR-010/FR-011 require every bypass (per-finding or full-outage) to be an
  explicit, attributed, recorded waiver — never silent or automatic. No new
  broadened credentials or scopes are introduced by this plan.

No violations requiring Complexity Tracking at this stage.

## Project Structure

### Documentation (this feature)

```text
specs/277-security-privacy-gate/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
.github/workflows/
├── security.yml          # extend: add sbom, provenance, dynamic-check jobs
└── codeql.yml             # unchanged (existing SAST gate)

verdict/
├── memory_gate.py         # existing memory boundary — target for PII/secret tests
├── memory_plane.py        # existing memory boundary — target for PII/secret tests
├── memory_bridge.py       # existing memory boundary — target for PII/secret tests
├── memory_*_adapter.py    # existing adapters — each gets a boundary test
└── (new) release/         # SBOM / provenance / dynamic-check orchestration helpers, if needed

docs/
├── privacy/               # (new) retention, erasure, and telemetry-consent policy docs
└── adr/                   # ADR for the launch-gate extension decision (SBOM/provenance
                            # format, dynamic-check tool, waiver mechanism)

tests/
├── security/               # (new) PII/secret boundary tests per memory/learning subsystem
├── privacy/                 # (new) retention/erasure + telemetry-consent tests
└── test_execution_packet_security.py  # existing — pattern reference for security tests
```

**Structure Decision**: Single-project layout (matches existing verdict-core
structure — no `src/` wrapper, package lives at repo root as `verdict/`). New
work is CI-workflow extension plus new test modules under `tests/security/` and
`tests/privacy/`, following the existing `tests/test_execution_packet_security.py`
pattern, plus policy documentation under `docs/privacy/`. No new source
project or service boundary is introduced.

## Complexity Tracking

*No Constitution Check violations. Table intentionally omitted.*

## Post-Design Constitution Check

*Re-evaluated after Phase 1 (data-model.md, contracts/, quickstart.md).*

- **I–V**: PASS, unchanged from the pre-design check. The data model adds no
  new persistent storage (evidence records are CI-run-scoped artifacts); the
  contract (`contracts/launch-gate-evidence.schema.json`) makes the
  no-silent-pass rule (FR-009) and the attributed-waiver rule (FR-010/FR-011)
  structurally explicit — `overall_status` cannot be `pass` while an unwaived
  `failed`/`unavailable`/`degraded` sub-result exists, and a
  `gate_unavailable`-scope waiver is schema-invalid unless
  `is_emergency_approver` is `true`. No new cross-repository coupling, new
  credentials, or broadened scopes were introduced in Phase 1. No Complexity
  Tracking entries are required.

**Extension hooks**: `.specify/extensions.yml` does not exist in this
worktree — no `after_plan` hooks to dispatch.
