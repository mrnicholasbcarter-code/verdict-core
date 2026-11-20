# Tasks

## 1. Implementation and focused verification

Ownership: story branch owner; verify with the focused tests named below.

- [x] 1.1 Add `admit_drops` to the `stages` dict in
      `verdict/intelligence.py` `_prepare_execution_path_request_at`, using
      `confirmed.exclusions` for reason codes and `"not_admitted"` as fallback.
- [x] 1.2 Add `"admit_drops"` to `TRACE_KEYS` in
      `tests/test_public_route_routing_trace.py` and update
      `_assert_matches_stages` and `test_trace_carries_ids_and_codes_only`.
- [x] 1.3 Add `test_admit_drops_covers_every_offered_route` invariant test in
      `tests/test_public_route_routing_trace.py`.
- [x] 1.4 Run focused tests, ruff check/format, mypy --strict,
      check_reachability, check_lint_gates.
- [x] 1.5 PR opened for independent review and release-train integration.
- [ ] (post-merge) Exercise `verdict route` or `POST /v1/route?include_routing_trace=true`
      against a real setup with cc/* candidates and confirm admit_drops names
      them with their passport codes; record raw output in evidence.
