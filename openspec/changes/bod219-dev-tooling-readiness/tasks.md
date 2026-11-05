# Tasks

- [x] Add `build` to the dev extra, relock with uv (only `build` and `pyproject-hooks` added) and add the proof-gate dev-dependency hygiene test.
- [x] Read the OpenSpec pin from `.github/workflows/ci.yml`; the strict-validation test runs `$OPENSPEC_BIN` or the PATH CLI only at that pin, with an isolated HOME, fails with the repair on another version and never downloads one; add the pin drift test over `.github/`, `scripts/` and `verdict/`.
- [x] Keep the skip when no OpenSpec CLI is installed at all (pre-existing behaviour: the CI unit-test job has no CLI and the required CI `lint` job runs the pinned strict validation), with the rationale in the test docstring.
- [x] Add the read-only `scripts/check_dev_readiness.py` with temporary-directory, fake-binary tests: a hook line that executes vgate, the ocr SHA-256 pin, credential-free probe environments (including `NPM_CONFIG_*` and `NETRC`) and a no-modification test.
- [x] Document the limits in the script docstring, `--help` and output: the pre-push check is static ("(static check)", dead-branch test), and probe sanitising covers environment variables only, not credential files under HOME.
- [x] Name the readiness check in CONTRIBUTING.md.
