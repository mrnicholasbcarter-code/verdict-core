# Quickstart: validating the security and privacy launch gate

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-09-01

This is a validation guide, not an implementation guide. Every scenario below proves one
acceptance property end to end and can be run offline. Implementation belongs in
`tasks.md`.

## Prerequisites

```bash
uv sync --extra dev --extra dashboard --extra server
```

Run tools through the environment explicitly. In a worktree a bare `pytest` or `python3`
resolves to the system interpreter and reports import errors that are artefacts of the
wrong interpreter, not real failures:

```bash
./.venv/bin/pytest -q
```

No scenario below requires network access, credentials, or a deployed service.

## Baseline the repository already enforces

Run before touching anything, so a later failure is attributable:

```bash
./.venv/bin/pytest -q
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
git diff --check
```

Running the suite must leave `git status` clean. If it does not, stop — something is
writing evidence into a tracked path, and gate results after that point are not
trustworthy.

## Scenario 1 — a release cannot ship an unresolved blocking finding (US1, FR-001–FR-004)

Prove the gate refuses rather than warns.

1. Generate the report against a fresh evidence directory:

   ```bash
   uv run python scripts/generate_gates_report.py --evidence-dir evidence
   uv run python scripts/verify_gates.py --evidence-dir evidence
   ```

2. Read `evidence/gates_status.json`. Expected: no gate reports `PASS` on absent
   evidence, per constitution quality gate 6.

3. Confirm no security check is advisory. Every step naming a scan must be free of
   `continue-on-error` and `|| true` — that is exactly what
   `generate_gates_report.py::_is_advisory` tests. See
   [contracts/gate-report.md](./contracts/gate-report.md).

**Expected outcome**: a finding at or above the declared severity fails the run. A finding
below it does not. The refusal-path test must also prove that no PyPI, npm, or GitHub
Release publish command was invoked.

## Scenario 2 — one threshold, discoverable in one place (FR-002)

```bash
uv run verdict compat manifest
```

**Expected outcome**: the emitted manifest carries `security_policy.blocking_severity`, and
that value is the only place the threshold is stated. Grepping the workflows for hardcoded
severity flags (`--audit-level`, `-ll`) should turn up nothing that contradicts it.

Today four checks enforce three different thresholds; see
[research.md](./research.md#threshold-incoherence-is-real-and-is-three-different-numbers).

## Scenario 3 — the exception file behaves correctly when broken (FR-005d–f)

Three cases, all of which must resolve toward blocking:

| Setup | Expected |
|---|---|
| Exception file absent | Every finding at or above the threshold blocks. |
| Exception file present but schema-invalid | Entries behave as absent **and** the invalid file is reported. |
| Entry present with a past `expires_on` | Behaves as absent; the finding blocks again. |

Validate a candidate file against the schema:

```bash
uv run python -c "
import json, jsonschema, pathlib
schema = json.loads(pathlib.Path('contracts/security-exceptions.schema.json').read_text())
doc = json.loads(pathlib.Path('security-exceptions.json').read_text())
jsonschema.validate(doc, schema)
print('valid')
"
```

**Expected outcome**: no path through these three cases silently allows a finding.

## Scenario 4 — a reviewer reproduces the evidence (US2, FR-016–FR-019)

```bash
uv run python scripts/generate_gates_report.py --evidence-dir evidence
```

Then inspect:

- `evidence/THREAT_MODEL.md` and `evidence/PRIVACY_POLICY.md` are present as **real files**.
- `evidence/gates_status.json` reports G5.1 and G5.2 as `PASS`.
- A fixture pull-request event that changes a security-sensitive path fails
  `scripts/check_threat_model_review.py` when the threat-model-review attestation is
  unchecked and passes only when it is explicitly checked.

**Expected outcome**: both gates move off `BLOCKED`. If the documents exist at the
repository root but the gates still report `BLOCKED`, the copy step is missing — artifacts
are resolved against the evidence directory, not the repository root, and symlinks are
rejected. See [contracts/gate-report.md](./contracts/gate-report.md).

## Scenario 5 — the gate's own tooling is pinned (FR-005a–c, SC-011)

```bash
grep -rn "uses:" .github/workflows/
```

**Expected outcome**: every third-party reference resolves to an immutable revision, with
the human-readable version in a trailing comment. Nothing resolves to a moving branch.

At the time of planning, 45 references were unpinned and
`pypa/gh-action-pypi-publish@release/v1` tracked a moving branch. The scanners themselves
were installed with `uv pip install pip-audit bandit`, unversioned; the pinning check
covers that too.

## Scenario 6 — private data does not cross a boundary (US3, FR-011–FR-014, SC-006)

The suite already forbids network access in the tests that assert this. Run them and
confirm no test needs a live endpoint to pass:

```bash
./.venv/bin/pytest -q tests/test_security.py
```

**Expected outcome**: telemetry stays local. The guarantee is enforced by a failing test,
not asserted in prose.

## Scenario 7 — erasure and the evidence chain coexist (FR-015a/b, SC-010)

The property under test: after an erasure, the mutable stores no longer hold the data, the
evidence chain still verifies, and what remains is non-reversible.

Composed from primitives that already exist — `MemoryPlane.tombstone`,
`ReceiptStore.tombstone`, `ReceiptStore.apply_retention`, and the redaction helpers in
`verdict/security.py`. See
[data-model.md](./data-model.md#non-reversible-reference).

**Expected outcome**: chain verification succeeds after the erasure record is appended.
A verification failure here means the erasure path rewrote history instead of appending to
it, which is the design error this scenario exists to catch.

## Scenario 8 — cross-repository parity (FR-020–FR-025)

Use three separately reviewed work units: initial `verdict-core`, then `verdict-node`, then
a fresh `verdict-core` coherence follow-up that updates ADR-024 and binds the final evidence.
Never share a commit or writer across repositories, and never reopen the merged Core feature
branch for the follow-up.

In `verdict-core`:

```bash
uv run verdict compat manifest > /tmp/manifest.json
uv run verdict compat check
```

In `verdict-node`, after its own pull request lands: run its declared `package.json`
scripts — build, test, lint, typecheck, package verification. Do not invent script names;
read them from the manifest and report anything missing honestly.

**Expected outcome**: a manifest carrying a policy the consumer cannot parse is rejected,
not ignored. A deliberate one-sided `blocking_severity` change is rejected with an error
that names both the expected and declared thresholds. Each repository's workflow must
gate its own artifacts and must not accept the other repository's result as evidence. A
version-1 reader rejects a version-2 manifest through the existing `schema_version` guard. See
[contracts/compatibility-manifest-v2.md](./contracts/compatibility-manifest-v2.md).

## What "done" looks like

| Check | Where proved |
|---|---|
| Blocking finding stops a release | Scenario 1 |
| One threshold, one place | Scenario 2 |
| Broken exception file fails closed | Scenario 3 |
| G5.1 and G5.2 off `BLOCKED` | Scenario 4 |
| Gate tooling pinned | Scenario 5 |
| No telemetry egress | Scenario 6 |
| Erasure leaves a verifiable chain | Scenario 7 |
| Both repositories on one policy | Scenario 8 |
