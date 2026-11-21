# Tasks

## 1. Remove ref-docs chain

- [x] 1.1 Delete `verdict/ref_docs.py` and `verdict/ref_docs_service.py`.
- [x] 1.2 Delete `tests/test_ref_docs.py`, `tests/test_ref_docs_integration.py`,
  `tests/test_ref_docs_service.py`, `tests/fixtures/ref_docs/`.
- [x] 1.3 Remove `_ref_docs_timer_row`, `_ref_docs_rows` and the `ref_docs_status`
  parameter from `verdict/doctor_diagnostics.py`.
- [x] 1.4 Remove `REF_DOCS` constant, `ORDER` entry, `_ACTIONS` entry, planning branch,
  apply branch from `verdict/setup_host.py`.
- [x] 1.5 Remove `_action_memory_ref_docs_*` functions from `verdict/actions/extra.py`
  and their registrations in `verdict/actions/registry.py`.
- [x] 1.6 Remove the `memory ref-docs` subparser from
  `verdict/commands/parsers_runtime.py` and `_cmd_memory_ref_docs` from
  `verdict/cli.py`.
- [x] 1.7 Update callers: `tests/test_doctor_host_prerequisites.py`,
  `tests/test_doctor_installed_state.py`, `tests/test_doctor_shared_memory.py`,
  `tests/test_cli_inprocess.py`, `tests/test_first_run_messages.py`,
  `tests/test_setup_host.py`, `tests/conftest.py`.
- [x] 1.8 Update docs: `docs/CLI_REFERENCE.md`, `docs/CONFIGURATION.md`,
  tombstone `docs/guides/reference-docs.md`.
- [x] 1.9 ADR-050, D33, `docs/adr/README.md` row.
  Verification: `uv run python -m pytest -q tests/test_doctor_host_prerequisites.py
  tests/test_doctor_installed_state.py tests/test_setup_host.py tests/test_ref_docs.py
  2>&1 | tail -3` → first three pass (last deleted → collected 0).

## 2. Update agent instructions

- [x] 2.1 `AGENTS.md`: "Memory first" rule uses Hindsight
  (`hindsight_search_pages` / `hindsight_read_page` / `hindsight_reflect`);
  "Legacy names" removes Hindsight from the removed list; note ai-memory removal.
- [x] 2.2 `ETHOS.md`: Decisions line updated to Hindsight; NOW updated to reflect the
  current state.
  Verification: `grep 'Hindsight' AGENTS.md ETHOS.md` → at least one hit per file.

## 3. Gates

- [x] 3.1 `uv run ruff check . && uv run ruff format --check .` → no findings.
- [x] 3.2 `uv run mypy verdict --strict` → no errors.
- [x] 3.3 `uv run python scripts/check_doc_links.py` → no broken links.
- [x] 3.4 `uv run python scripts/check_reachability.py --ratchet-base origin/main` → no
  new findings, no orphan modules.
- [x] 3.5 `uv run python scripts/check_lint_gates.py` → all gates pass (openspec
  validate, open tasks, reachability, DOD, proof-path trailers).
  Verification: gate output ends `All gates passed.`

## 4. Post-merge live check

- [ ] (post-merge) 4.1 After installed-main is reinstalled from merged main and the old
  binary removed the ref-docs timer (`verdict memory ref-docs install-service
  --uninstall`; `systemctl --user daemon-reload`), `verdict doctor` is green with
  `ai-memory.service` stopped.
