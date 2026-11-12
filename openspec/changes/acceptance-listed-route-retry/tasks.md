# Tasks

## 1. Implementation

- [x] 1.1 Add retry-once for listed routes in `verdict/acceptance_toolcheck.py::measure`, both attempts logged with attempt tags
- [x] 1.2 Add red-first tests in `tests/test_acceptance.py`; show failing without fix, green after
- [x] 1.3 Run focused `tests/test_acceptance.py`, `ruff check`, `ruff format --check`, `mypy --strict verdict/acceptance_toolcheck.py`
- [x] 1.4 Independent cross-family review of the diff (APPROVE, antigravity/gemini-3-flash)
- [ ] (post-merge) 1.5 Push through vgate; CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 From installed merged main, run `verdict eligibility --json --gateway http://127.0.0.1:20128` and record that listed-route failures are retried once and precision meets the 95% bar
- [ ] (post-merge) 2.2 Re-run the same acceptance command after the freshness window (next hourly `verdict-acceptance` run) with no manual action; record raw output
