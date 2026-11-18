# Tasks

## 1. Implementation

- [x] 1.1 Write failing tests in `tests/test_certification_rehearsal_workflow.py`: preflight argv is FREE-only and bounded; zero passing routes blocks orchestrate; a failed preflight never echoes child output
- [x] 1.2 Add the bounded `identify --screen --capacity free` preflight to `scripts/run_certification_rehearsals.py`, failing closed with `RehearsalConfigurationError`
- [x] 1.3 Give `tests/test_certification_failure_diagnostics.py` a passing preflight in the real screen schema so it still tests orchestration-failure diagnostics
- [x] 1.4 Verify: both rehearsal test modules, ruff, ruff format, mypy --strict, doc links, reachability and both OpenSpec validators pass
- [ ] (post-merge) 1.5 Dispatch `certify-rehearsal.yml` on merged main and verify the clean rehearsal gets past planning with an attested receipt
