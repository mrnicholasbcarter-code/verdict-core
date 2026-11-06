# Tasks

Owned files: `verdict/ocr_release.py`, `verdict/setup_host.py`,
`scripts/check_dev_readiness.py`, `.github/workflows/certify-rehearsal.yml`,
`docs/CLI_REFERENCE.md`, `CONTRIBUTING.md`, the test anchor in
`openspec/changes/bod219-setup-host-prerequisites/specs/setup-host-prerequisites/spec.md`;
`tests/test_ocr_release.py`, `tests/test_setup_host.py`,
`tests/test_doctor_host_prerequisites.py`, `tests/test_check_dev_readiness.py`,
`tests/test_wheel_contents.py`, `tests/test_certification_rehearsal_workflow.py`;
this change.

## 1. Tests first
- [x] 1.1 Seven new tests ran against unchanged production code on `124a37e6`.
  Result: 7 failed, 100 passed across `tests/test_ocr_release.py`,
  `tests/test_setup_host.py`, `tests/test_doctor_host_prerequisites.py`,
  `tests/test_check_dev_readiness.py` and `tests/test_wheel_contents.py`. The
  failures:
  - The installed-layout import and the extracted wheel both raised
    `FileNotFoundError ... site-packages/.github/workflows/certify-rehearsal.yml`.
  - Doctor returned `warning`, `cannot verify ocr: [Errno 2] ...`, the live symptom.
  - Setup planned `['host.metadata']` first, with no `host.ocr`.
  - Readiness printed `repair: fix OCR_VERSION/OCR_SHA256 in ...`.
  - The workflow drift test found `OCR_VERSION`/`OCR_SHA256` in the job `env`.
  - The digest test found no `OCR_SHA256` constant.

## 2. One pin in the package
- [x] 2.1 `OCR_VERSION` and `OCR_SHA256` are constants in `verdict/ocr_release.py`,
  and `ocr_pin()` returns them. `HostEnvironment.ocr_pin` replaces `repo_root`,
  and `check_ocr()` takes no root. The unreadable-pin warning and setup's silent
  skip are removed. Messages and docs name `verdict.ocr_release`.
- [x] 2.2 `certify-rehearsal.yml` reads the pin from the package after `uv sync`
  and writes it to `$GITHUB_ENV`. The job `env` no longer holds it. The drift test
  runs that step in `bash -euo pipefail` against a `.venv/bin/python` shim and
  checks the exported values.
- [x] 2.3 After: 104 passed on the same five files. The 3 tests that injected a pin
  through a fake workflow file were removed, and the real-workflow test is now
  `test_pin_is_the_package_constants`. 383 passed across those files plus
  `tests/test_certification_rehearsal_workflow.py`,
  `tests/test_certification_failure_diagnostics.py`,
  `tests/test_check_openspec_tasks.py`, `tests/test_cli_inprocess.py`,
  `tests/test_doctor_shared_memory.py`, `tests/test_visual_system.py`,
  `tests/test_home_action_rendering.py`, `tests/test_check_reachability.py` and
  `tests/test_check_doc_links.py`. `ruff check`, `ruff format --check`,
  `mypy verdict --strict`, `bandit -r verdict -ll` and doc links are clean.
  `check_reachability.py --ratchet-base origin/main` reports 0 new findings.
- [x] 2.4 Wheel: `python -m build --wheel` into a temporary directory produced
  `verdict_core-0.5.0-py3-none-any.whl`. It contains `verdict/ocr_release.py` with
  both constants and no `.github` entry. An `-I` interpreter importing the
  extracted wheel returned the pin from `<tmp>/site/verdict/ocr_release.py`. The
  build output was removed.

## 3. Live acceptance (pending; needs merged main)
AGENTS.md "Definition of done" part 1 requires both checks. Neither can run
before merge: the first needs an installed build of merged main, and the
`rehearse` job runs only when `github.ref == 'refs/heads/main'`. Nothing below
has run.
- [ ] 3.1 Install merged main non-editable on the operator host and run
  `verdict doctor`. The ocr row must be `ok`, or `failed` with the pinned
  version, and never `cannot verify ocr`. With ocr missing, `verdict setup`
  must plan `host.ocr`. Record the date, SHA, command and raw output.
- [ ] 3.2 Dispatch `certify-rehearsal.yml` on main. The new step must export the
  package pin, and `sha256sum --check --strict` must pass in the reviewer
  install step. Record the run URL.
