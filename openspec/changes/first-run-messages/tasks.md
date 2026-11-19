# Tasks

## 1. Serve
- [x] 1.1 Missing token or receipts path: one line, exit 2, server never starts; configured server still starts: `tests/test_first_run_messages.py`

## 2. Doctor
- [x] 2.1 Absent parts before setup are `not-configured`, still blocking; broken parts and post-setup rows stay `failed`: `tests/test_first_run_messages.py`
- [x] 2.2 First line points to `verdict setup`; exit stays 1: `tests/test_first_run_messages.py`

## 3. Orchestrate
- [x] 3.1 Missing-proof BLOCKED reason names the bounded screen command, which the real parser accepts; no route admitted: `tests/test_first_run_messages.py`

## 4. Docs
- [x] 4.1 docs/CONFIGURATION.md: serve settings and First run section

- [ ] (post-merge) On a fresh HOME from merged main, `verdict serve` and `verdict doctor` print the new first-run lines
