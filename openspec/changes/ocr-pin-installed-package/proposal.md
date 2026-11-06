# The ocr pin ships in the package

## Why
On the operator host Verdict is installed as a regular, non-editable package in
`.venv/lib/python3.13/site-packages`. There `verdict doctor` reported
`warning | Review tool (ocr) | cannot verify ocr: [Errno 2] No such file or directory: '<venv>/lib/python3.13/site-packages/.github/workflows/certify-rehearsal.yml'`.

`verdict/ocr_release.py` read `OCR_VERSION` and `OCR_SHA256` from
`.github/workflows/certify-rehearsal.yml` under `package_root()`, the directory
above the package. That file exists only in a repository checkout, and the tests
only ran from one. So in an installed package the doctor ocr row could not
verify ocr. `verdict setup` planned no `host.ocr` action at all, because a
missing pin was treated as "nothing to do", and it said nothing.
`scripts/check_dev_readiness.py` also depended on the workflow file under
`--root`.

## What Changes
- `verdict/ocr_release.py` holds the pin as module constants `OCR_VERSION` and
  `OCR_SHA256`. `ocr_pin()` returns them and takes no root. The module is part
  of the `verdict` package, so the wheel carries the pin with no package-data
  setting.
- `.github/workflows/certify-rehearsal.yml` no longer restates the pin. After
  `uv sync`, a new step `Read the ocr pin from the package` runs
  `.venv/bin/python -c 'from verdict.ocr_release import ...'` and writes both
  values to `$GITHUB_ENV`. The install step still checks the length,
  downloads `$OCR_VERSION` and runs `sha256sum --check --strict` against
  `$OCR_SHA256`.
- `HostEnvironment` drops `repo_root`, which existed only to find the workflow.
  It gains an `ocr_pin` seam that defaults to `ocr_release.ocr_pin`.
  `check_dev_readiness.check_ocr()` takes no root.
- The pin can no longer be unreadable, so the doctor `warning | cannot verify ocr`
  row and setup's silent skip are removed. Messages name `verdict.ocr_release`
  as the pin source instead of the workflow file.
- Drift checks in `tests/test_ocr_release.py`. One runs the workflow's pin step
  the way the runner does and requires it to export exactly the package
  constants. It also requires that no `env` block sets `OCR_VERSION` or
  `OCR_SHA256`, that the workflow has no 64-hex literal, and that the steps run
  sync, then read, then install. The other requires that no tracked file outside
  frozen history (`docs/proof/`, archived OpenSpec changes) contains the digest,
  except `verdict/ocr_release.py`. Both skip when `.github/` or `.git` is absent.

## Design choice
The workflow checks out the exact source SHA and runs `uv sync` before the step
that needs the pin. It can therefore read the pin from the package it is about to
certify. That leaves one value, and the check enforces that nothing else holds a
copy.

The alternative was to keep literal values in the workflow and test that they
equal the package constants. That leaves two copies, which a bump must edit
together. The pin's trust boundary does not change: the value comes from the
same checked-out SHA as before. `.github/CODEOWNERS` gives `/verdict/` and
`/.github/` the same owner, so the pin's review protection does not change
either.

## Capabilities
### Modified Capabilities
- `setup-host-prerequisites`: "The ocr pin has one source and one check" now
  names `verdict/ocr_release.py` as the source and requires that an installed
  package resolves the pin.

## Impact
- Code: `verdict/ocr_release.py`, `verdict/setup_host.py`,
  `scripts/check_dev_readiness.py`, `.github/workflows/certify-rehearsal.yml`.
- Tests: `tests/test_ocr_release.py`, `tests/test_setup_host.py`,
  `tests/test_doctor_host_prerequisites.py`, `tests/test_check_dev_readiness.py`,
  `tests/test_wheel_contents.py`, `tests/test_certification_rehearsal_workflow.py`.
- Docs: `docs/CLI_REFERENCE.md` (pin source; the unreadable-pin warning is gone).
- The requirement being modified is added by the active change
  `bod219-setup-host-prerequisites`, which must be archived first. Its scenario
  anchor `tests/test_ocr_release.py::test_pin_is_read_from_the_real_rehearsal_workflow`
  is replaced by `::test_pin_is_the_package_constants` in this delta.
- Not changed: `package_root()` still builds the doctor repair for a missing
  prime-sync timer (`cp <package_root>/deploy/systemd/...`). In an installed
  package that path does not exist either. That is a separate defect and is not
  fixed here.
- Not claimed, and left for the DoD evidence after merge:
  - Install merged main non-editable on the operator host and run
    `verdict doctor`. The ocr row must be `ok`, or `failed` with the pinned
    version, and never `cannot verify ocr`. With ocr missing, `verdict setup`
    must plan `host.ocr`.
  - Run `certify-rehearsal.yml` on main. It must install the reviewer from the
    package pin, and `sha256sum --check --strict` must pass.
