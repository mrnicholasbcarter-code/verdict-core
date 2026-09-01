# Implementation Plan: Cross-Repository Security and Privacy Launch Gate

**Branch**: `238-security-privacy-launch-gate` | **Date**: 2026-09-01 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/238-security-privacy-launch-gate/spec.md`

## Summary

Most of what issue #238 asks for already exists in `verdict-core` and is already
non-advisory. The work is not "add security scanning" — it is closing five specific gaps
and one contradiction:

1. **Two documents do not exist.** `THREAT_MODEL.md` and `PRIVACY_POLICY.md` are absent, so
   gates G5.1 and G5.2 report `BLOCKED`. Writing them is necessary but not sufficient: gate
   artifacts resolve against the *evidence directory*, not the repository root, and
   symlinks are rejected. An evidence step must copy them.
2. **No bill of materials exists anywhere**, for any published artifact.
3. **Three checks enforce three different thresholds** — `pip-audit` blocks on any
   advisory, `bandit -ll` on medium, `npm audit --audit-level=high` on high. There is no
   single declared severity a reviewer can read.
4. **The gate's own tooling is unpinned on two axes.** Forty-five action references, zero
   pinned to an immutable revision, one (`pypa/gh-action-pypi-publish@release/v1`) on a
   moving branch; and the scanners themselves installed by an unversioned
   `uv pip install pip-audit bandit`.
5. **`verdict-node` has no security workflow at all**, and no TypeScript reader for the
   policy the manifest will carry.

The contradiction: the spec's erasure requirement met an append-only evidence chain.
Clarification resolved it — erasure clears the mutable stores while the chain keeps
non-reversible references and is never rewritten. Research then showed the primitives to
build that on already exist (`MemoryPlane.tombstone`, `ReceiptStore.tombstone`,
`apply_retention`, the redaction helpers), so this is composition, not new machinery.

Two pieces of absorbed scope were confirmed at clarification time and are carried here
deliberately: completing ADR-024's CI wiring (the `verdict compat` commands exist, fail
closed, and are referenced by no workflow), and pinning a release pipeline where nothing is
pinned today.

## Technical Context

**Language/Version**: Python ≥3.10 (CI matrix 3.10, 3.11, 3.12, 3.13); TypeScript on Node ≥18 for the `verdict-node` half
**Primary Dependencies**: existing — `pip-audit`, `bandit`, `npm audit`, OSV scanner, CodeQL, `jsonschema` (declared `dev`); added — a CycloneDX generator, a pinned history secret-scanner. No new runtime dependency.
**Storage**: no new store. Existing `MemoryPlane` and `ReceiptStore` (append-only); the exception file and the bill of materials are tracked/generated files, not records in a store.
**Testing**: `pytest` via `./.venv/bin/pytest` (a bare `pytest` in a worktree resolves to the wrong interpreter); `ruff check`, `ruff format --check`, `mypy verdict --strict`; existing `tests/test_generate_gates_report.py`, `test_verify_gates.py`, `test_launch_gates.py`, `test_security.py`, `test_compatibility_manifest.py`, `test_release_workflow.py`
**Target Platform**: GitHub Actions (ubuntu runners) for enforcement; the library itself is offline and credential-free
**Project Type**: control-plane library with a CLI and an optional server extra, plus a release/evidence pipeline; cross-repository with `verdict-node`
**Performance Goals**: pull-request feedback must not regress. Dependency, secret, and static checks stay on every pull request; the bill of materials and dynamic verification run on the scheduled and release runs, which are the runs that gate shipping.
**Constraints**: offline — no network, no credentials in tests; the suite must leave `git status` clean; evidence goes to ignored directories only; one repository per commit; every gate check must be non-advisory (no `continue-on-error`, no `|| true`) or `generate_gates_report.py` counts it as not enforcing
**Scale/Scope**: 25 functional requirements, 11 success criteria, 3 user stories, 2 repositories, 10 workflow files, ~45 action references to pin

No `NEEDS CLARIFICATION` remains. Four were resolved in the clarification session; the rest
were resolved by reading the code (see [research.md](./research.md)).

## Constitution Check

Checked against `.specify/memory/constitution.md` v1.1.0 before Phase 0 and re-checked after
Phase 1 design. **Result: PASS, both times.** No violations, so Complexity Tracking is empty.

| Principle | How this plan satisfies it |
|---|---|
| **I. Coordination is governance, execution is delivery** | The deliverable is enforcement in CI and code, not a plan document or an issue comment. Gate status comes from `gates_status.json` produced by the pipeline, which outranks any claim made in a transcript. |
| **II. Documentation before dependencies** | Phase 0 read `generate_gates_report.py`, `verify_gates.py`, `acceptance-gates.yml`, `security.yml`, `compatibility_manifest.py`, `memory_plane.py`, `receipt_store.py`, and ADR-024 before any design choice. Two designs that sound right were discarded because the code says otherwise: gate artifacts resolve against the evidence directory, and erasure primitives already exist. |
| **III. Repository boundaries are non-negotiable** | `verdict-core` and `verdict-node` land in separate pull requests, never one commit. The interface is the compatibility manifest, its owner is `verdict-core`, per-repo validation is each repository's own CI, and the rollout order is core-then-node — all identified here, before the first dependent change merges. |
| **IV. Verification is part of the change** | Every requirement lands with a check that fails when the property is absent. No universal numeric threshold is invented: the blocking severity is a declared, versioned policy value, not a number chosen here. Failures and skips get reported as themselves. |
| **V. Safety, reversibility, least authority** | The exception file's field set transcribes the constitution's own governance clause rather than inventing one. Every failure mode resolves toward blocking: absent, malformed, and expired all behave as "no exception". Erasure appends a tombstone rather than deleting, so it is auditable. No credentials are added; publication and merge remain gated. |

Quality gates 1–6 are satisfied by construction: the checks are repository-native (gate 1);
acceptance and failure paths both map to evidence, and Scenario 3 in the quickstart exists
specifically to prove the failure paths (gate 2); the manifest change is a cross-repository
interface and carries contract and compatibility validation (gate 3); this is risk-based
security work with the release pipeline as the affected budget (gate 4); merge stays
gated on clean status and green checks on the exact head (gate 5); and gate 6 — never
report an unknown as a pass — is the reason G5.1 and G5.2 must actually move off `BLOCKED`
rather than be re-declared (gate 6).

**One thing this plan deliberately does not do**: add a rejection branch for the
"version-1 reader, version-2 manifest" case. `__post_init__` already raises on any
`schema_version != "1"`, so that path fails closed today. Adding a second path around a
working guard would weaken it.

## Project Structure

### Documentation (this feature)

```text
specs/238-security-privacy-launch-gate/
├── spec.md
├── plan.md                 # this file
├── research.md             # Phase 0
├── data-model.md           # Phase 1
├── quickstart.md           # Phase 1 — validation scenarios
├── contracts/              # Phase 1
│   ├── security-exceptions.schema.json
│   ├── compatibility-manifest-v2.md
│   └── gate-report.md
└── checklists/
    └── requirements.md
```

### Source Code (repository root)

```text
verdict-core/
├── THREAT_MODEL.md                     # new — published deliverable, named by G5.1
├── PRIVACY_POLICY.md                   # new — published deliverable, named by G5.2
├── contracts/
│   └── security-exceptions.schema.json # new — validates the exception file
├── verdict/
│   ├── compatibility_manifest.py       # extend: security policy in the hashed region, schema v2
│   ├── security.py                     # extend: severity normalisation, exception evaluation
│   ├── memory_plane.py                 # unchanged — tombstone() already present
│   └── receipt_store.py                # unchanged — tombstone()/apply_retention() already present
├── scripts/
│   └── generate_gates_report.py        # extend: G5.3 needle table, new scan classes
├── .github/workflows/
│   ├── security.yml                    # thresholds unified, scans added, tooling pinned
│   ├── acceptance-gates.yml            # copy the two documents into evidence/
│   ├── release.yml                     # bill of materials + attestation; drop the moving branch ref
│   └── *.yml                           # every third-party reference pinned to an immutable revision
├── tests/
│   ├── test_generate_gates_report.py   # extend
│   ├── test_security.py                # extend: normalisation, exception failure modes
│   ├── test_compatibility_manifest.py  # extend: schema v2, fail-closed cases
│   └── test_release_workflow.py        # extend: pinning, bill of materials
└── docs/adr/
    └── ADR-028-*.md                    # new — latest is ADR-027

verdict-node/                           # SEPARATE PULL REQUEST, lands second
├── .github/workflows/security.yml      # new — no security workflow exists there today
├── contracts/                          # new — TypeScript reader for the policy field
└── security-exceptions.json            # its own exception file
```

**Structure Decision**: no new module, no new store, no new top-level directory in
`verdict-core`. The feature is delivered by extending three files that already own the
relevant behaviour — `generate_gates_report.py` owns what a gate means,
`compatibility_manifest.py` owns what crosses the repository boundary, and `security.py`
owns redaction and severity — plus the workflow files and two published documents. This
follows the pattern that worked for feature 004: wire authority into the boundaries that
already exist rather than introducing a new gate that has to be kept in step with them.
The one genuinely new artifact in the repository is the exception file and its schema,
which has no existing owner.

The `verdict-node` half is a separate pull request against a separate repository, sequenced
after `verdict-core` publishes the policy, per Constitution III.

## Complexity Tracking

No constitutional violations. This section is intentionally empty.
