# BOD-219: dev tooling readiness

## Why
Restoring the operator host on 2026-11-05 found three silent gaps. The proof
contract's `build` gate (`python -m build --wheel`) failed because `build` was
not in the `[dev]` extra. `tests/test_check_openspec_tasks.py` ran whatever
`openspec` was on PATH, so a local 1.16.0 failed a check that CI's pinned
1.13.2 passes. The `vgate` push gate script was missing and the `pre-push` hook
was not executable, so pushes would have skipped the gate with no warning.

## What Changes
- `build` joins `[project.optional-dependencies].dev`; `uv.lock` adds `build`
  and `pyproject-hooks` only (lock revision 3 kept). A hygiene test requires
  every `python -m <module>` proof gate to be a dev dependency.
- The OpenSpec pin in `.github/workflows/ci.yml` is the single source of truth.
  The strict-validation test runs the PATH `openspec` only when it reports
  that pin, otherwise `npx --yes @fission-ai/openspec@<pin>`, and fails (never
  skips) when the pinned CLI cannot run. A test requires every OpenSpec pin in
  workflows, scripts and `verdict/` to equal the CI pin. CI is unchanged.
- New read-only `scripts/check_dev_readiness.py` reports `vgate` and the
  `pre-push` hook, the local `openspec` version against the CI pin, `build`
  in the project interpreter, and `ocr` against the version pinned in
  `.github/workflows/certify-rehearsal.yml`. It exits 1 with an exact repair.
- CONTRIBUTING.md names the readiness check in Setup.

## Capabilities
### New Capabilities
None. This change is repository tooling (`skip_specs: true`).
### Modified Capabilities
None.

## Impact
`pyproject.toml`, `uv.lock`, `scripts/check_dev_readiness.py`, three test
files and CONTRIBUTING.md. No product code under `verdict/` and no CI workflow
changes. The check never edits `vgate`, which stays operator-owned.
