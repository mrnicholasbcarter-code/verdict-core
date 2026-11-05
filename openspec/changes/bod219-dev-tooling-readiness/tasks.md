# Tasks

- [x] Add `build` to the dev extra, relock with uv (only `build` and `pyproject-hooks` added) and add the proof-gate dev-dependency hygiene test.
- [x] Read the OpenSpec pin from `.github/workflows/ci.yml`; the strict-validation test runs `$OPENSPEC_BIN` or the PATH CLI only at that pin, fails with the repair on another version, skips when no CLI is installed and never downloads one; add the pin drift test over `.github/`, `scripts/` and `verdict/`.
- [x] Add the read-only `scripts/check_dev_readiness.py` with temporary-directory, fake-binary tests: a hook line that executes vgate, the ocr SHA-256 pin, credential-free probe environments and a no-modification test.
- [x] Name the readiness check in CONTRIBUTING.md.
