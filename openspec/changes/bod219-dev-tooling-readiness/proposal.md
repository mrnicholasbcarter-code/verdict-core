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
  The strict-validation test runs `$OPENSPEC_BIN`, else the PATH `openspec`,
  only when it reports that pin, with an isolated HOME. Another version fails
  the test with the repair (install the pin, or point `OPENSPEC_BIN` at a
  pinned binary), so a drifted local CLI can never pass or skip it. Tests
  never download a CLI. A test requires every OpenSpec pin in text files under
  `.github/` (workflow `*.yml` and `*.yaml`), `scripts/` and `verdict/`
  (vendored code excluded) to equal the CI pin. CI is unchanged.
- With no OpenSpec CLI installed at all, the strict-validation test still
  skips. This keeps the behaviour CI has always relied on and is not a
  weakening: the CI unit-test job installs no OpenSpec CLI, and the required
  CI `lint` job runs the pinned
  `npx --yes @fission-ai/openspec@1.13.2 validate --all --strict --no-interactive`
  itself, so strict validation still gates every pull request.
- New read-only `scripts/check_dev_readiness.py` reports `vgate`, a `pre-push`
  hook with a non-comment line that executes it, the local `openspec` version
  against the CI pin, `build` in the project interpreter, and `ocr` against
  the version and SHA-256 pinned in `.github/workflows/certify-rehearsal.yml`.
  It exits 1 with an exact repair. Known limits, stated in its docstring and
  `--help`:
  - The pre-push check is static. It cannot prove the hook reaches vgate (a
    vgate line inside `if false; then ... fi` passes), so it reports "invokes
    vgate (static check)". vgate itself is the enforcement point; a test pins
    this dead-branch case as a static pass.
  - Probe sanitising covers environment variables only: credential names
    (KEY, TOKEN, SECRET, PASSWORD, CREDENTIAL), `OPENAI_`/`ANTHROPIC_`/
    `OMNIROUTE_`/`NPM_CONFIG_` prefixes and `NETRC` are dropped. Credential
    files reachable through HOME, XDG directories, `~/.npmrc` or `~/.netrc`
    are not hidden. The probes are local `--version`, `git rev-parse` and
    `python -m build --version` commands; nothing is downloaded.
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
