# Tasks

- [x] Add red-first unit/CLI/setup/doctor tests for the replacement flow.
- [x] Render separate passive and daily screen units with approved bounds.
- [x] Wire explicit private passive persistence and comma-separated capacity filters.
- [x] Replace the setup warm-cache installer and doctor timer/freshness checks.
- [x] Complete focused tests and static checks (387 passed; mypy/ruff/collection/reachability/spec validation green).
- [x] Parent review and push acceptance: cross-family review APPROVE (NVIDIA Nemotron, /home/nick/dev/work/identify-correction/pr935-review.md) and push gate PASS (vgate: PASS, /home/nick/dev/work/identify-correction/timer-pr-push.txt).
- [ ] Proposed BOD-345 split-auth implementation: keep six fixed source slots and at most six bounded GETs; mark missing credentials source-local unavailable without network; resolve stripped `OMNIROUTE_MANAGEMENT_TOKEN` from its same-named secure-store field only; keep management fallback to legacy `api_key` only when management is absent, never after failure; confine management auth to passive fetch.
- [ ] Add focused security tests for missing management/both credentials, rejected management tokens, 401 no-fallback/no-retry, sentinel redaction in reports/saved reports/logs, exact six GETs/no POST-write-model calls, unchanged limits/redirects, and honest empty/unknown inference catalogs while preserving historical observations.
- [ ] Update passive identify call sites without changing policy or budget behavior; no new secret file or compatibility check.
