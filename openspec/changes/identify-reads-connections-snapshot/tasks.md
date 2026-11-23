# Tasks

## 1. Implementation

- [x] 1.1 Failing tests: restricted key plus valid snapshot gives FREE routes and routes_checked > 0; no snapshot gives 0; stale, tampered or missing snapshot fails with a named error
- [x] 1.2 Read the snapshot in `fetch_omniroute_evidence` with `read_connections_snapshot`; skip management reads; map rows through the existing usability and capacity rules
- [x] 1.3 Failing test: `source_errors` in the preflight summary holds names and kinds only, never URL or key
- [x] 1.4 Add `source_error_kinds` to identify and `source_errors` to `preflight_summary`
- [ ] (post-merge) the certify-rehearsal workflow preflight checks more than 0 routes
