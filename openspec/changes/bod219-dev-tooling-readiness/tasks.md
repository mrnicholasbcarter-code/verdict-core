# Tasks

- [x] Add `build` to the dev extra, relock with uv (only `build` and `pyproject-hooks` added) and add the proof-gate dev-dependency hygiene test.
- [x] Read the OpenSpec pin from `.github/workflows/ci.yml`; run the pinned CLI in the strict-validation test (PATH binary on a match, `npx` otherwise, fail when unavailable) and add the pin drift test.
- [x] Add the read-only `scripts/check_dev_readiness.py` with temporary-directory, fake-binary tests, including a no-modification test.
- [x] Name the readiness check in CONTRIBUTING.md.
