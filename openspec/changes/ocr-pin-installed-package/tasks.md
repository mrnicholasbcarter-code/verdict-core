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

## 3. Cross-family review
- [x] 3.1 Round 1 (`cx/gpt-5.6-luna-high`, REQUEST_CHANGES), two valid findings:
  - The doctor and setup tests in the installed layout ran the checkout's modules
    with `package_root()` redirected. New
    `tests/test_wheel_contents.py::test_installed_wheel_runs_the_ocr_doctor_row_and_setup_action`
    runs the doctor ocr row and the setup ocr action from the extracted wheel in
    an `-I` interpreter with a temporary `HOME`, no ocr on `PATH` and a fake
    fetch. Red on `124a37e6` with only this test copied in:
    `AssertionError: assert 'warning' == 'failed'`. Green after the fix.
  - The requirement said the package is the only copy of the release tag, which
    recorded ocr output and test pins contradict, and only the digest was
    checked. The requirement now says "only definition", and new
    `tests/test_ocr_release.py::test_no_other_tracked_file_defines_the_pin`
    fails on any other literal `OCR_VERSION`/`OCR_SHA256` assignment. Red on
    `124a37e6`: it listed `.github/workflows/certify-rehearsal.yml`. Green after.
  The five ocr test files: 106 passed. The archive order it also raised is task 4.3.

## 4. Live acceptance and archive (pending; needs merged main)
AGENTS.md "Definition of done" requires all three: part 1 for 4.1 and 4.2,
part 5 for 4.3. None can run before merge: 4.1 needs an installed build of
merged main, the `rehearse` job runs only when
`github.ref == 'refs/heads/main'`, and 4.3 needs the evidence file. Nothing
below has run.
- [ ] 4.1 Install merged main non-editable on the operator host and run
  `verdict doctor`. The ocr row must be `ok`, or `failed` with the pinned
  version, and never `cannot verify ocr`. With ocr missing, `verdict setup`
  must plan `host.ocr`. Record the date, SHA, command and raw output.
- [ ] 4.2 Dispatch `certify-rehearsal.yml` on main. The new step must export the
  package pin, and `sha256sum --check --strict` must pass in the reviewer
  install step. Record the run URL.
- [ ] 4.3 After the DoD evidence file passes, archive
  `bod219-setup-host-prerequisites` first, then this change. Until then that
  change's spec delta still names the workflow as the pin source, and this
  MODIFIED delta cannot apply.
