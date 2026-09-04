# Phase 0 Research: Cross-Repository Security and Privacy Launch Gate

**Feature**: [spec.md](./spec.md) | **Branch**: `238-security-privacy-launch-gate` | **Date**: 2026-09-01

All findings below were read out of the repository at `origin/main` @ `08e0bf7`. Nothing
here is inferred from a name or a filename. Where a claim is about behaviour, the exact
symbol and line are cited so a reviewer can re-check it.

## How the existing gate actually works

Before deciding anything, the mechanism the spec builds on had to be understood, because
two plausible-sounding designs are wrong against it.

`scripts/generate_gates_report.py` computes gate statuses; `scripts/verify_gates.py` only
validates the resulting `gates_status.json` (schema, canonical JSON, evidence paths). Any
new *check* is therefore a change to the generator, not the verifier.

The generator judges "is this check advisory?" per workflow step:

```
_is_advisory(step) == ("continue-on-error" in step) or ("|| true" in step)
_workflow_steps(body) splits on  ^\s*- (name|uses):
```

G5.3 is driven by a needle table:

| Requirement | Needle |
|---|---|
| python dependency audit | `pip-audit` |
| npm dependency audit | `npm audit` |
| osv scanner | `osv-scanner` |
| static analysis | `codeql` |

A missing needle yields `BLOCKED`; a needle whose every occurrence is advisory yields
`FAIL`; otherwise `PASS`. Today all four are present and non-advisory, so G5.3 passes.

**Decision**: new scan classes (bill of materials, secret scanning, dynamic verification)
are added by extending that table rather than by inventing a parallel mechanism.
**Rationale**: the report is already the single artifact a reviewer reads, and the
advisory-detection logic is the property the spec's FR-001/FR-002 depend on.
**Alternatives considered**: a standalone `scripts/verify_security.py` — rejected, it
would produce a second source of truth the release evidence does not carry.

## The finding that changes the shape of the work

`Gate("G5.1", …, artifacts=("THREAT_MODEL.md",))` and the G5.2 equivalent are resolved by
`_resolve_artifacts`, which computes `path = evidence_dir / artifact` and rejects
symlinks. The artifacts are **not** resolved against the repository root.

`.github/workflows/acceptance-gates.yml` starts with `rm -rf evidence evidence_bundle`
and then populates `evidence/` from advisory steps before the non-advisory generator and
verifier run.

**Decision**: authoring the two documents is necessary but not sufficient. An evidence
step must copy them into `evidence/` with a real copy.
**Rationale**: without the copy the documents exist, a reviewer can read them, and G5.1
and G5.2 still report `BLOCKED` — the exact failure mode the spec is meant to remove.
**Alternatives considered**: (a) symlinking — rejected, `_resolve_artifacts` has an
explicit `is_symlink` guard; (b) changing the artifact tuple to a repo-relative path —
rejected, it would diverge from how every other gate resolves evidence.

## Where the two documents live

**Decision**: `THREAT_MODEL.md` and `PRIVACY_POLICY.md` at the repository root, copied
into `evidence/` by the acceptance-gates workflow.
**Rationale**: the gate names those paths, and the root already holds the published
peers (`README.md`, `SECURITY.md`, `ACCEPTANCE_GATES.md`). These are shipped deliverables,
not working files, so the "no working files at repo root" rule does not apply.
**Alternatives considered**: `docs/` — rejected, it would require changing the gate
definition and would separate them from `SECURITY.md`, which readers arrive at first.

## Threshold incoherence is real and is three different numbers

| Check | Command | Effective threshold |
|---|---|---|
| Python dependencies | `uv run pip-audit --local` | any advisory at all |
| Python static analysis | `uv run bandit -r verdict -ll -s B108` | medium and above |
| Node dependencies | `npm audit --omit=dev --audit-level=high` | high and above |
| OSV | reusable workflow, `osv-scanner.toml` | config-defined |

**Decision**: one declared blocking severity, expressed once, with each tool configured
to that severity; where a tool cannot express the threshold natively, its output is
filtered by a shared evaluator rather than by a second hardcoded flag.
**Rationale**: FR-002 requires the threshold to be discoverable in one place; today a
reviewer has to read four command lines in two files to learn what blocks a release.
**Alternatives considered**: leaving `pip-audit` maximally strict — rejected, "stricter
than declared" is still undeclared, and it is the reason exceptions get invented ad hoc.

## The gate's own tooling is unpinned on two axes

Forty-five `uses:` references across ten workflow files; **zero** are pinned to an
immutable revision. Third-party references:

| Reference | Where | Kind of ref |
|---|---|---|
| `astral-sh/setup-uv@v7` | security, benchmark, release | mutable tag |
| `google/osv-scanner-action/…@v2.5.1` | security | mutable tag |
| `github/codeql-action/{init,analyze}@v4` | codeql | mutable tag |
| `pypa/gh-action-pypi-publish@release/v1` | release | **moving branch** |

Separately, `security.yml:33` runs `uv pip install pip-audit bandit` with no version
constraint, and neither tool is declared in `pyproject.toml`'s `dev` extra.

**Decision**: pin both axes — actions to commit revisions with the human-readable version
in a trailing comment, and the security tools as declared, locked dependencies rather
than an ad-hoc install.
**Rationale**: FR-005a says the tools that decide whether a release ships must themselves
be identified by an immutable revision. A floating `pip-audit` decides that too.
**Alternatives considered**: pinning only third-party actions — rejected, it leaves the
larger hole (the scanners) open while claiming the property.

**Resolved versions (reviewed 2026-09-04)**: `pip-audit==2.10.1` and
`bandit==1.9.4`, the current PyPI releases supporting Python 3.10+. Both become exact
`dev` dependencies and are locked by `uv.lock`; workflow installation from an unbounded
package name is removed.

## Erasure composes primitives that already exist

An earlier reading of this area was too strong. The primitives are present:

| Symbol | File |
|---|---|
| `MemoryPlane.tombstone` | `verdict/memory_plane.py` |
| `ReceiptStore.tombstone`, `ReceiptStore.apply_retention` | `verdict/receipt_store.py` |
| `redact_text`, `redact_sensitive_dict` | `verdict/security.py` |
| `_enforce_retention` | `verdict/adaptive_state.py` |

**Decision**: the erasure path composes these rather than introducing a new primitive; the
work is a governed entry point over them plus proof that the evidence chain still verifies
afterwards.
**Rationale**: tombstoning is append-only by construction, which is precisely the property
FR-015a needs. Writing a deleter would contradict the ledger design in ADR-017.
**Alternatives considered**: physical deletion with chain re-linking — rejected, it
destroys the property that makes the receipts evidence.

## Carrying the severity policy across repositories

`verdict/compatibility_manifest.py` holds `CompatibilityManifest(schema_version,
manifest_hash, contracts)`. `manifest_hash` covers `contracts` only, and `__post_init__`
raises unless `schema_version == "1"`.

**Decision**: add the security policy to the manifest and bump the schema version.
**Rationale**: the version guard means a reader that predates the policy field rejects the
new manifest outright. Fail-closed behaviour for FR-025 therefore falls out of the
existing invariant instead of needing new rejection code. The policy must be inside the
hashed region, or a manifest could be re-signed with a weaker threshold.
**Alternatives considered**: a sidecar policy file — rejected, it is not covered by
`manifest_hash` and could drift from the manifest it claims to describe.

`verdict compat manifest` and `verdict compat check` exist (`verdict/cli.py:2553-2561`) and
fail closed. They are referenced by **no** workflow, and ADR-024 records itself as
"Partially Implemented — verdict-core side complete …; downstream repo declarations and CI
wiring still open." Completing that wiring is FR-024, absorbed scope confirmed at
clarification time.

## Cross-repository parity has no existing surface

No TypeScript mirror of the manifest exists in `contracts/` or `verdict/client-sdk/`, and
`verdict-node` has `ci`, `codeql`, `lint`, and `npm-publish` workflows but **no**
`security.yml` at all.

**Decision**: `verdict-core` lands first and publishes the policy; `verdict-node` follows
in its own pull request with its own security workflow and a TypeScript reader for the
policy field.
**Rationale**: Constitution III forbids one commit spanning both repositories and requires
the interface, per-repo validation, and ordered rollout to be identified before the first
dependent change merges. The manifest is that interface.
**Alternatives considered**: simultaneous landing — not available; they are separate
repositories with separate CI.

## Dynamic verification scope

The spec scopes this to the optional server surface (`verdict/server*`, the `server`
extra: FastAPI and uvicorn). Nothing dynamic runs today.

**Decision**: exercise the running server surface with malformed and hostile input in a
test that starts the app in-process, asserting it refuses rather than crashes or leaks.
**Rationale**: keeps the check inside the existing pytest gate — no new service to stand
up in CI, no network, and it inherits the offline constraint the suite already enforces.
**Alternatives considered**: an external scanner against a deployed instance — rejected,
it needs a deployment CI does not have and would make the check non-reproducible offline.

## Cadence and budget

`security.yml` already triggers on `push` to main, `pull_request`, a weekly `schedule`,
and `workflow_dispatch`.

**Decision**: new checks inherit that trigger set. The bill of materials and dynamic
verification run on the scheduled and release runs; dependency, secret, and static checks
stay on every pull request.
**Rationale**: keeps pull-request feedback fast while ensuring nothing ships unscanned,
since the release path runs the full set.
**Alternatives considered**: everything on every pull request — rejected on latency with
no gain, because the release run is the one that gates shipping.

## Pre-publication workflow boundary

The current `.github/workflows/release.yml` is one job containing build, attestation,
preflight, and all three publication surfaces. The acceptance-gates workflow also runs on
`release: published`, which is necessarily too late to authorize that release.

**Decision**: split the release workflow into `verify-release` and `publish-release` jobs.
The first job has read/preparation authority only, builds the exact candidate artifacts,
assembles every non-document gate input and emits a canonical source-SHA-bound
`compatibility-manifest-v2.json`. T045 then adds the real-file threat/privacy-document copy
step, and T048 adds exception evidence plus the final generator/verifier placement after both
steps; only then is `gates_status.json` generated and verified. The job uploads a SHA-bound
`release-candidate` artifact with digests. The publication job attaches the canonical manifest
and its digest to the immutable GitHub release so the Node work unit has a public, attested
compatibility-fixture source. The second job alone has registry/OIDC
write authority, depends unconditionally on `verify-release`, downloads and re-verifies the
candidate, preflights immutable targets, and publishes without rebuilding. The scheduled
acceptance workflow remains retrospective reporting, not release authority.
**Rationale**: GitHub Actions dependencies exist between jobs, not between arbitrary steps.
Splitting at the first irreversible operation makes a nonzero verifier exit prevent the
publication job from being scheduled and proves that the bytes verified are the bytes
published.
**Alternatives considered**: inserting a verifier step into the monolithic release job was
rejected because it leaves evidence assembly implicit and mixes write authority with
preparation; using the `release: published` acceptance workflow was rejected because the
registry mutation has already happened; rebuilding in the publication job was rejected
because it breaks exact-artifact provenance.

## Threat-model review enforcement

FR-019 applies only when a pull request changes a trust boundary, persistence format,
provider adapter, or execution path. Documentation in `SECURITY.md` alone cannot enforce
that pre-merge condition.

**Decision**: add a repository-owned `scripts/check_threat_model_review.py` check. It reads
the GitHub pull-request event payload, classifies the changed paths against the documented
security-sensitive path set, and requires the exact checked attestation from
`.github/pull_request_template.md` when any sensitive path changes. The job is wired into
the existing required CI workflow without `continue-on-error` or `|| true`. Non-PR events
report the check as not applicable rather than inventing a passing review.
**Rationale**: the decision is reproducible with fixture event payloads, does not depend on
an untracked branch-protection setting, and leaves explicit review evidence on the pull
request while keeping the merge decision in the existing CI gate.
**Alternatives considered**: prose-only guidance was rejected because it cannot block a
merge; CODEOWNERS alone was rejected because its enforcement depends on external branch
protection state; a new hosted review service was rejected because it would violate the
offline and least-authority constraints.

## Exception file format

Constitution governance already dictates the fields: an exception records scope,
rationale, approver, evidence, expiry or follow-up, and affected repositories.

**Decision**: a tracked file validated against a schema, with those fields required, and
`jsonschema` (already a declared `dev` dependency) as the validator.
**Rationale**: the constitution is the authority here, so the schema transcribes it rather
than inventing a shape. Reusing the declared validator adds no dependency.
**Alternatives considered**: free-form entries in `SECURITY.md` — rejected, FR-005e
requires a malformed entry to behave as absent, which needs a machine-checkable schema.

**Evidence writer**: `verdict/security.py::write_active_exceptions_evidence` owns canonical
filtering and serialization. It accepts the validated exception document plus the build
clock, emits only schema-valid unexpired entries with every required field, and writes
`evidence/security_exceptions.json`. Both acceptance and release workflows invoke this
helper; neither workflow reimplements expiry or schema logic in shell.

## Cross-repository delivery sequence

**Decision**: use three work units: the initial Core implementation PR, the Node parity PR,
then a Core coherence follow-up PR. The Node worktree is created only after Core is merged
and the version-2 manifest is independently observed in the authorized registry. The final
Core worktree starts from then-current `origin/main` after Node lands and is the sole owner
of ADR-024's final status and cross-repository evidence.
**Rationale**: ADR-024 cannot truthfully become complete in the initial Core PR because the
Node consumer does not exist yet. Returning to the already-merged Core feature branch would
also blur source identity. A fresh follow-up PR preserves one writer, one repository, and
exact remote ancestry for every claim.
**Alternatives considered**: a simultaneous cross-repository commit was rejected by
Constitution III; marking ADR-024 complete before Node lands was rejected as false evidence;
amending the merged Core branch was rejected because it is not a fresh integration base.

## Resolved without further clarification

- Bill-of-materials format and Python generator: CycloneDX using
  `cyclonedx-bom==7.3.1`. The official CycloneDX Python project documents the
  `cyclonedx-py` CLI and supports Python 3.9+, so it fits the repository's Python
  3.10–3.13 matrix. It is a pinned build-only dependency and writes JSON into the
  evidence directory; it is not a runtime dependency.
- Node generator: `@cyclonedx/cyclonedx-npm@6.0.1`. Its official documentation requires
  Node `>=20.18.0` and npm `>=9`, so generation runs in a dedicated Node 22 release job;
  this does not raise `verdict-node`'s consumer runtime floor of Node 18. The command
  runs once per published package and writes one JSON SBOM per artifact.
- Secret scanning tool: `gitleaks/gitleaks-action` release `v3.0.0`, pinned by immutable
  commit `e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e`, with
  `GITLEAKS_VERSION=8.30.1`. Official documentation says v3 uses Node 24, supports full
  history when checkout uses `fetch-depth: 0`, and requires no Gitleaks license for a
  personal-account repository. PR comments and SARIF upload are disabled so the scan is
  a deterministic blocking check rather than an advisory side channel.

**Documentation evidence (reviewed 2026-09-04)**:

- <https://github.com/CycloneDX/cyclonedx-python> and release `v7.3.1`
- <https://github.com/CycloneDX/cyclonedx-node-npm> and release `v6.0.1`
- <https://github.com/gitleaks/gitleaks-action> and release `v3.0.0`

**Alternatives considered**: a generic CycloneDX GitHub Action was rejected because the
language-native CLIs make the package-to-SBOM mapping explicit and locally reproducible;
a hand-written history grep was rejected because it is neither a secret-scanner contract
nor a maintainable ruleset; floating action tags and `latest` tool versions were rejected
because FR-005a requires immutable identity.

## Open items deliberately left to `/speckit-tasks`

None. No `NEEDS CLARIFICATION` remains in the Technical Context.
