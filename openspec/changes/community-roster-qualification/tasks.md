# Tasks — native community roster discovery and qualification

## 1. Source

- [x] 1.1 Reproduce the zero-row parse and the lost provider namespace against the real upstream file; add an attributed frozen fixture.
- [x] 1.2 Parse the active roster without executing JavaScript; keep the pinned fetch, add a current-revision fetch by commit SHA, and reject an empty or invalid roster.

Verify: `pytest tests/test_discovery_sources.py`.

## 2. Native report and screen

- [x] 2.1 Add `--community-roster` to `verdict identify`: account for every entry and name why each one is not screened, including unregistered models on configured providers.
- [x] 2.2 Narrow the bounded screen to matching reachable routes; keep a list price from deciding qualification unless the operator passes `--assume-unmetered-connections`.
- [x] 2.3 Keep a model-scoped billing refusal from stopping sibling models; keep account-wide and rate-limit stops.

Verify: `pytest tests/test_identify_screen.py tests/test_identify_cli.py`.

## 3. Verification

- [x] 3.1 Focused and neighbouring tests, ruff, mypy strict, doc links, strict OpenSpec validation and the lint gates.
- [ ] 3.2 Independent review of the diff.
- [ ] (post-merge) 4.1 On installed main: record a `--community-roster` passive report, a bounded roster screen, eligibility for a discovered route, and a verified coding-task receipt through it.
