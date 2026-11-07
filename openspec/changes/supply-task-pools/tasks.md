# Tasks

## 1. Implementation

- [x] 1.1 Add red-first tests for the candidates source, supply, task pools and the doctor
  report row.
- [x] 1.2 Read `/v1/auto-combo/auto/candidates` as the seventh bounded GET with the inference
  key and parse usable rows into live candidates (`verdict/omniroute_evidence.py`).
- [x] 1.3 Build `supply` and `task_pools` in `verdict/identify_pools.py` and add them to the
  passive report with a read-only health-cache tool-proof snapshot (`verdict/identify.py`).
- [x] 1.4 Summarize the implementation pool and supply in the doctor identify report row
  without changing its ok/failed rules (`verdict/identify_service.py`).
- [x] 1.5 Document the seventh GET and the `supply` and `task_pools` keys in
  `docs/CLI_REFERENCE.md`.
- [x] 1.6 Run the focused suites, `ruff check`, `ruff format --check`, `mypy --strict`,
  `scripts/check_doc_links.py`, `scripts/check_reachability.py` and
  `openspec validate supply-task-pools --strict`.
- [x] 1.7 Independent cross-family review of the diff.
- [x] 1.8 Retain parsed quota windows and model cooldowns on each provider row of `supply`,
  name a connection with no window `limits_not_reported:<connection id>`, and keep a failed
  limits source as the single `source_unavailable:limits` gap. Window retention does not
  change connection usability or pool admission.

## 2. Live checks

- [ ] (post-merge) 2.1 From merged main, run `verdict identify --passive --json` on the
  operator host and record that seven GETs ran, `supply.complete` and its named unknowns, and
  the implementation pool counts.
- [ ] (post-merge) 2.2 After the passive timer has run from merged main, record the raw
  `verdict doctor` identify report row showing the pool and supply summary.
