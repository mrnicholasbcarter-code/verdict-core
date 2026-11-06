## MODIFIED Requirements

### Requirement: The ocr pin has one source and one check
`OCR_VERSION` and `OCR_SHA256` in `verdict/ocr_release.py` MUST be the only copy
of the ocr release tag and of the SHA-256 of its linux/amd64 asset. The pin ships
inside the package and MUST resolve from an installed package with no repository
checkout. `verdict setup`, `verdict doctor`, `scripts/check_dev_readiness.py` and
`.github/workflows/certify-rehearsal.yml` MUST read the pin through
`verdict.ocr_release`. The workflow MUST NOT restate it, and no other tracked file
outside frozen history (`docs/proof/`, archived OpenSpec changes) MUST contain the
digest. A binary MUST count as the pinned release only when its SHA-256 equals
the pin. Setup MUST hash the downloaded asset before writing and MUST NOT install
it on a mismatch.

#### Scenario: Readiness and doctor share the pin
- **WHEN** the readiness script, setup or doctor needs the ocr pin
- **THEN** each uses `verdict.ocr_release.ocr_pin` and `inspect_ocr`, and the pin is the module constants
- **TEST** `tests/test_ocr_release.py::test_readiness_script_shares_the_helper`,
  `::test_pin_is_the_package_constants`,
  `tests/test_check_dev_readiness.py::test_ocr_check_reads_the_pin_without_the_rehearsal_workflow`

#### Scenario: An installed package reads the pin without a checkout
- **WHEN** `verdict` is imported from a `site-packages` directory with no `.github/`, including one extracted from the built wheel
- **THEN** `ocr_pin()` returns the pin; the doctor ocr row reports a missing binary as `failed` with the pinned version and the `verdict setup --apply --allow host.ocr` repair; setup plans `host.ocr`, fetches the pinned release URL and refuses bytes that do not hash to the pin
- **TEST** `tests/test_ocr_release.py::test_installed_package_reads_the_pin_without_a_checkout`,
  `tests/test_wheel_contents.py::test_installed_wheel_reads_the_ocr_pin_without_a_checkout`,
  `tests/test_doctor_host_prerequisites.py::test_installed_package_reports_missing_ocr_against_the_pin`,
  `tests/test_setup_host.py::test_installed_package_plans_and_runs_the_ocr_install_with_the_pin`

#### Scenario: Nothing can drift from the package pin
- **WHEN** the rehearsal workflow's pin step runs as the runner runs it
- **THEN** it exports exactly the package `OCR_VERSION` and `OCR_SHA256`, after `uv sync` and before the install step; no workflow `env` sets either value, the workflow holds no 64-hex literal, and no other tracked file outside frozen history contains the digest
- **TEST** `tests/test_ocr_release.py::test_rehearsal_workflow_reads_the_pin_from_the_package`,
  `::test_no_other_tracked_file_restates_the_pin_digest`

#### Scenario: A digest mismatch never installs
- **WHEN** the downloaded asset does not hash to `OCR_SHA256`
- **THEN** nothing is written and the existing binary is unchanged
- **TEST** `tests/test_ocr_release.py::test_install_never_writes_on_a_digest_mismatch`,
  `tests/test_setup_host.py::test_an_ocr_digest_mismatch_never_installs`
