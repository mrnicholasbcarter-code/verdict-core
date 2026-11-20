# Tasks

## 1. bootstrap_fix
Owner: bootstrap_fix
Verify: python -m pytest -q tests/test_capability_bootstrap.py
- [x] 1.1 Add probe_gateway to doctor_capability_report and pass it to run_bootstrap

## 2. diagnostics_fix
Owner: diagnostics_fix
Verify: python -m pytest -q tests/test_doctor_capability_gateway_probe.py
- [x] 2.1 Add _doctor_capability_gateway_probe and wire it into _collect_doctor_diagnostics

## 3. probe_spec
Owner: probe_spec
Verify: openspec validate doctor-capability-gateway-probe --strict
- [x] 3.1 Write the OpenSpec change
