# Data Model

Each launch gate is one row in `RELEASE_CHECKLIST.md`:

| Field | Meaning | Required before approval |
|---|---|---|
| Gate | CI, Bandit, dependency audit, CodeQL/OSV, package smoke, or docs smoke | yes |
| Source revision | exact commit evaluated | yes |
| Evidence URL/command | reproducible hosted URL or local command | yes |
| Result | PASS or FAIL | yes |
| Limitation | scope, unavailable hosted evidence, or known issue | yes |
| Reviewer/date | accountable reviewer and UTC date | yes |

`Launch decision` remains `PENDING EVIDENCE` until every required row has
evidence and a reviewer signs off. Empty rows are not implicit passes.
