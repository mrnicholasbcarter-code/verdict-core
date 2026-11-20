# Tasks

## 1. Implementation

- [x] 1.1 Add fail-closed guard in `verdict/intelligence.py::_offload_free_tier` for relay callers when OmniRoute snapshot is unavailable
- [x] 1.2 Add regression tests in `tests/test_authoritative_eligibility.py`: snapshot unavailable on relay path fails closed; snapshot unavailable without dispatch context uses historical path
- [x] 1.3 Run focused `tests/test_authoritative_eligibility.py`, `ruff check`, `ruff format --check`, `mypy --strict verdict`
- [x] 1.4 Run `uv run python scripts/check_lint_gates.py`; push with vgate
- [ ] (post-merge) 1.5 CI exact-head checks green on PR head; squash-merge to main

## 2. Live checks

- [ ] (post-merge) 2.1 From installed merged main, run the hourly acceptance command and confirm the serve process obtains a snapshot within the 15-minute LKG window; record raw output
- [ ] (post-merge) 2.2 Confirm acceptance timer next hourly run completes without a "missing route-bound admission proof" error; record raw output
