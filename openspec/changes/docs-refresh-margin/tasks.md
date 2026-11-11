# Tasks

## 1. Renewal margin
- [x] 1.1 Fresh outside margin untouched, inside margin renewed, expired renewed, missing still red in doctor, read-only ignores the margin, margin configurable: `tests/test_documentation_preflight.py`
- [x] 1.2 `renew_margin_seconds` and `renewed` in `verdict/documentation_preflight.py`

## 2. Timer
- [x] 2.1 `verdict/docs_refresh_service.py`, `verdict memory docs --install-service`: `tests/test_docs_refresh_service.py`
- [x] 2.2 `host.docs-refresh-timer` in `verdict setup`: `tests/test_setup_host.py`
- [x] 2.3 Doctor row: `tests/test_doctor_host_prerequisites.py`

## 3. Docs
- [x] 3.1 `docs/CLI_REFERENCE.md`; `ETHOS.md` NOW line (operator wording, BOD-339)

- [ ] (post-merge) Install with `verdict setup --apply --allow host.docs-refresh-timer` after the operator approves; doctor shows the row ok
- [ ] (post-merge) After the first timer run, `verdict doctor` documentation preflight stays ready past the 24 h mark with no `--fix` by hand
