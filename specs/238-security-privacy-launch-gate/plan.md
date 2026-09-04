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

Release refusal is enforced by splitting `.github/workflows/release.yml` at the irreversible
boundary. A `verify-release` job performs every non-publishing action (it holds `id-token: write` and `attestations: write` for provenance, `contents: read`, and no `environment`): checks out the
tag, runs tests and security checks, builds the exact Python/npm artifacts, generates their
SBOMs and attestations, assembles every non-document evidence input, and emits a canonical
`compatibility-manifest-v2.json` bound to the release source SHA. T038 creates this boundary;
after T043/T044 author the policy documents, T045 adds their real-file copy step to
`verify-release` and to the retrospective acceptance workflow. T048 adds exception evidence
and owns the final placement of `scripts/generate_gates_report.py` and
`scripts/verify_gates.py` after both evidence steps, so all 29 gates are evaluated against the
complete candidate. `publish-release` attaches that canonical manifest to the immutable GitHub
release with its digest.
It uploads one SHA-bound `release-candidate` workflow artifact containing the release assets,
evidence, manifest, and digests. A separate `publish-release` job has the
registry/OIDC write permissions, has an unconditional `needs: verify-release`, downloads and
re-verifies that exact candidate, performs immutable-target preflight immediately before the
first write, then publishes to npm, PyPI, and GitHub Releases. Only `publish-release` carries `environment: pypi` and `contents: write`/`packages: write`. The npm SBOM step runs inside `verify-release` under Node 22, not as a third job. No publication command exists
in `verify-release`, and no build or evidence regeneration occurs in `publish-release`. The security-policy handoff is explicit: a step output carries `security_policy.blocking_severity` to the following scan/evaluator steps; no job-level environment assignment is used. Native scanner flags are applied only where supported, and the shared gate-report evaluator normalises the remaining outputs.

`.github/workflows/acceptance-gates.yml` remains the scheduled/manual/post-release reporting
surface. Its `release: published` trigger is retrospective and is never treated as the
pre-publication authority. The release workflow reuses the same generator and verifier
contracts but assembles its own complete exact-tag evidence before publication.

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

**Language/Version**: Python ≥3.10 (CI matrix 3.10, 3.11, 3.12, 3.13); TypeScript on Node 22 for the `verdict-node` half (new CI/release jobs target Node 22)
**Primary Dependencies**: existing scanners made reproducible — `pip-audit==2.10.1` and `bandit==1.9.4`; existing platform checks — `npm audit`, OSV scanner, CodeQL, and `jsonschema` (declared `dev`); added build-only tools — `cyclonedx-bom==7.3.1` for Python, `@cyclonedx/cyclonedx-npm@6.0.1` in a Node 22 release job, and `gitleaks/gitleaks-action@e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e` (`v3.0.0`) with `GITLEAKS_VERSION=8.30.1`. No new runtime dependency.
**Storage**: no new store. Existing `MemoryPlane` and `ReceiptStore` (append-only); the exception file and the bill of materials are tracked/generated files, not records in a store.
**Testing**: `pytest` via `./.venv/bin/pytest` (a bare `pytest` in a worktree resolves to the wrong interpreter); `ruff check`, `ruff format --check`, `mypy verdict --strict`; existing `tests/test_generate_gates_report.py`, `test_verify_gates.py`, `test_launch_gates.py`, `test_security.py`, `test_compatibility_manifest.py`, `test_release_workflow.py`
**Target Platform**: GitHub Actions (ubuntu runners) for enforcement; the library itself is offline and credential-free
**Project Type**: control-plane library with a CLI and an optional server extra, plus a release/evidence pipeline; cross-repository with `verdict-node`
**Performance Goals**: pull-request feedback must not regress. Dependency, secret, and static checks stay on every pull request; the bill of materials and dynamic verification run on the scheduled and release runs, which are the runs that gate shipping.
**Constraints**: offline — no network, no credentials in tests; the suite must leave `git status` clean; evidence goes to ignored directories only (Core ignores `evidence/*.json`; `evidence_bundle/` and the Node `evidence/` directory need ignore rules added by T085 and T069); one repository per commit; every gate check must be non-advisory (no `continue-on-error`, no `|| true`) or `generate_gates_report.py` counts it as not enforcing
**Scale/Scope**: 33 functional-requirement keys (25 numbered requirements plus 8 lettered subrequirements), 11 success criteria, 3 user stories, 2 repositories, 10 workflow files, ~45 action references to pin

No `NEEDS CLARIFICATION` remains. Four were resolved in the clarification session; the rest
were resolved by reading the code (see [research.md](./research.md)).

## Constitution Check

Checked against `.specify/memory/constitution.md` v1.1.0 before Phase 0 and re-checked after
Phase 1 design. **Result: PASS, both times.** No violations, so Complexity Tracking is empty.

| Principle | How this plan satisfies it |
|---|---|
| **I. Coordination is governance, execution is delivery** | The deliverable is enforcement in CI and code, not a plan document or an issue comment. Gate status comes from `gates_status.json` produced by the pipeline, which outranks any claim made in a transcript. |
| **II. Documentation before dependencies** | Phase 0 read `generate_gates_report.py`, `verify_gates.py`, `acceptance-gates.yml`, `security.yml`, `compatibility_manifest.py`, `memory_plane.py`, `receipt_store.py`, and ADR-024 before any design choice. Two designs that sound right were discarded because the code says otherwise: gate artifacts resolve against the evidence directory, and erasure primitives already exist. |
| **III. Repository boundaries are non-negotiable** | `verdict-core` and `verdict-node` land in separate pull requests, never one commit. The interface is the compatibility manifest, its owner is `verdict-core`, per-repo validation is each repository's own CI, the rollout order is core-then-node, and the rollback path is: revert the Core PR (version-1 readers stay fail-closed on a v2 manifest) and, if the `v0.3.0` tag was pushed, leave the immutable release in place with no registry mutation reversed — all recorded in ADR-028 before the first dependent change merges. |
| **IV. Verification is part of the change** | Every requirement is assigned an implementation or validation task, including refused publication with no registry mutation, cross-repository provenance, one-sided policy mismatch, and threat-model review enforcement. No universal numeric threshold is invented: the blocking severity is a declared, versioned policy value, not a number chosen here. Failures and skips get reported as themselves. |
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
├── security-exceptions.json            # new — tracked, initially empty; the file FR-005 requires
├── contracts/
│   └── security-exceptions.schema.json # new — validates the exception file
├── verdict/
│   ├── compatibility_manifest.py       # extend: security policy in the hashed region, schema v2
│   ├── security.py                     # extend: severity normalisation, exception evaluation
│   │                                      and write_active_exceptions_evidence()
│   ├── memory_plane.py                 # unchanged — tombstone() already present
│   └── receipt_store.py                # unchanged — tombstone()/apply_retention() already present
├── scripts/
│   ├── generate_gates_report.py        # extend: G5.3 needle table, new scan classes
│   └── check_threat_model_review.py    # new: fail PRs missing required review attestation
├── .github/workflows/
│   ├── security.yml                    # thresholds unified, scans added, tooling pinned
│   ├── acceptance-gates.yml            # copy the two documents into evidence/
│   ├── release.yml                     # split into verify-release / publish-release; SBOM + attestation; drop the moving branch ref
│   └── *.yml                           # every third-party reference pinned to an immutable revision
├── .github/
│   └── pull_request_template.md        # new — create explicit threat-model-review attestation
├── tests/
│   ├── test_generate_gates_report.py   # extend
│   ├── test_security.py                # extend: normalisation, exception failure modes
│   ├── test_compatibility_manifest.py  # extend: schema v2, fail-closed cases
│   ├── test_release_workflow.py        # extend: pinning, bill of materials, refused publication
│   ├── test_server_dynamic.py           # new: optional-server adversarial verification
│   └── test_threat_model_review.py     # new: changed-path and missing-attestation cases
└── docs/adr/
    └── ADR-028-*.md                    # new — latest is ADR-027

verdict-node/                           # SEPARATE PULL REQUEST, lands second
├── .github/workflows/security.yml      # new — no security workflow exists there today
├── contracts/security-exceptions.schema.json # new — copied schema
├── src/compatibility-manifest.ts       # new — TypeScript reader for the policy field
├── src/security-exceptions.ts          # new — schema/expiry evaluation
├── security-exceptions.json            # its own exception file
└── release workflow                    # SBOM + provenance for every npm artifact
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

Clean-tree evidence is written separately as `evidence/clean-tree-status.json` and bundled
with the release evidence. It is never inserted manually into `gates_status.json`; that
report remains generator-owned under the gate-report contract.

Delivery uses three explicitly separate repository work units:

1. **Core implementation PR** in the current feature worktree: Phases 1–6, including ADR-028,
   Core validation, merge, and separately authorized publication of the version-2 manifest.
2. **Node parity PR** in `/home/nick/dev/verdict-node/.worktrees/238-verdict-node`: Phase 7, starting only
   after the Core merge and registry proof, with tests before implementation and Node-native
   verification before merge/publication.
3. **Core coherence follow-up PR** in `/home/nick/dev/verdict-core/.worktrees/238-core-coherence`: Phase 8,
   created from then-current `origin/main` only after Node lands. It updates ADR-024, records
   the cross-repository evidence, and runs the final eight-scenario coherence check.

Each PR has one writer and its own clean-tree, tests, CI, merge, and publication receipts.
Publication remains a separately authorized operation; a completed implementation PR does
not imply that either registry was mutated.

## Complexity Tracking

No constitutional violations. This section is intentionally empty.
