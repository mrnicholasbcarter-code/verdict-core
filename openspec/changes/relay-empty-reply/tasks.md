# Tasks

## 1. Specification

Write the proposal, design and spec delta before touching production code. Verify:
`npx @fission-ai/openspec@1.13.2 validate relay-empty-reply --strict` and the vendored
validator both pass.

- [x] 1.1 Write proposal.md, design.md, tasks.md and the spec delta with scenarios
- [x] 1.2 Both OpenSpec validators pass

## 2. Tests first

Write failing tests before the fix. Verify: each new test is red on the unmodified code
and the failure message names the unmet condition (not an import error or crash).

- [x] 2.1 `tests/test_relay_truncated_empty_reply.py`: detector unit tests and relay integration tests (a)–(d)
- [x] 2.2 Tests cover: empty-length failover with failure_class recorded, partial answer pass-through, tool call pass-through, all-candidates-exhausted bounded error
- [x] 2.3 Multi-choice (n>1) detector tests: choice-0-truncated-but-choice-1-has-content (must not match), all-choices-truncated (must match), non-Mapping entry (must not match)
- [x] 2.4 Serve-budget test: captures the probe body via the open_no_redirect seam and asserts max_tokens == 256

## 3. Implementation

Apply the relay fix and the serve-budget fix. Verify: run
`uv run python -m pytest -q tests/test_relay_truncated_empty_reply.py tests/test_acceptance.py`
— all tests including the new ones must pass.

- [x] 3.1 `is_truncated_empty_reply` in `verdict/orchestration/provider_catalog.py` (all-choices check)
- [x] 3.2 `_truncated_empty_body(surface)` in `verdict/api.py`
- [x] 3.3 `truncated_empty` detection block in relay loop, projecting 200 → 422
- [x] 3.4 `previous_refused = inband_refusal or truncated_empty` for idempotency
- [x] 3.5 Break condition extended with `truncated_empty`
- [x] 3.6 `attempts_used` and `record_attempt_event` use `failure_class: "truncated_empty_reply"`
- [x] 3.7 `_SERVE_PROBE_MAX_TOKENS = 256` added to `verdict/acceptance.py`; probe body updated

## 4. Gates

Verify: `uv run ruff check . && uv run ruff format --check .`, `uv run mypy verdict --strict`,
`uv run python scripts/check_lint_gates.py`, and `vgate` all pass.

- [x] 4.1 Focused tests pass, inband and failure-feedback tests show no regression
- [x] 4.2 ruff check, ruff format, mypy --strict
- [x] 4.3 Doc links, reachability ratchet, lint gates, vgate

## 5. Live verification

Verify: on installed main, the hourly acceptance `serve` check passes on
`antigravity/gemini-3.5-flash-lite` (the previously failing route).

- [ ] (post-merge) 5.1 Install merged main and confirm hourly acceptance `serve` check passes on `antigravity/gemini-3.5-flash-lite`
