# Tasks

Owned files: `verdict/doctor_diagnostics.py` (`_doctor_gateway_key` and its one call),
`tests/test_doctor_gateway_key.py`, the healthy-host fixture in `tests/test_cli_inprocess.py`
and this change.

- [x] 1.1 Add `_doctor_gateway_key` and call it after the gateway lifecycle row.
- [x] 1.2 Focused tests for OK, 401/403, missing metadata, timeout/transport/non-200/garbage, 404
  and no key; the check never crashes doctor.
