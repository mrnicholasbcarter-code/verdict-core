# Tasks

## 1. Local implementation

- [x] 1.1 Add parametrized test in `tests/test_admission.py` covering all opaque and concrete cases.
- [x] 1.2 Add admission test that `kilocode/kilo-auto/free` in inventory is dropped at DISCOVERED with reason `opaque_route`.
- [x] 1.3 Update `is_opaque` in `verdict/admission.py` to recognize opaque model segments after provider prefix.
- [x] 1.4 Ensure `test_opaque_routes_are_never_admitted` stays green.
- [x] 1.5 Run all gates: pytest, ruff, mypy, doc links, reachability, OpenSpec validators, git diff check.

## 2. Post-merge verification (operator)

- [x] (post-merge) Install from merged main and confirm `verdict eligibility --json` no longer lists `kilocode/kilo-auto/free`, `kilocode/kilo-auto/small` or `kilocode/openrouter/free` as TASK_ELIGIBLE. Verify by running `verdict eligibility --json` on operator's live setup and test that these route IDs are absent from TASK_ELIGIBLE entries.
  Done 2026-11-21 on installed-main df4cb61c: all three routes stop at DISCOVERED with reason `admission:opaque_route`; none is TASK_ELIGIBLE. Evidence: docs/proof/dod/opaque-nested-routes.json.