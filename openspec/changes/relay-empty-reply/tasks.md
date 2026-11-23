# Tasks

## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta with scenarios
- [x] 1.2 `openspec validate relay-empty-reply --strict` passes

## 2. Tests first

- [x] 2.1 `tests/test_relay_truncated_empty_reply.py`: detector unit tests and relay integration tests (a)–(d)
- [x] 2.2 Tests cover: empty-length failover, partial answer pass-through, tool call pass-through, all-candidates-exhausted bounded error

## 3. Implementation

- [x] 3.1 `is_truncated_empty_reply` in `verdict/orchestration/provider_catalog.py`
- [x] 3.2 `_truncated_empty_body(surface)` in `verdict/api.py`
- [x] 3.3 `truncated_empty` detection block in relay loop, projecting 200 → 422
- [x] 3.4 `previous_refused = inband_refusal or truncated_empty` for idempotency
- [x] 3.5 Break condition extended with `truncated_empty`
- [x] 3.6 `attempts_used` and `record_attempt_event` use `failure_class: "truncated_empty_reply"`

## 4. Gates

- [x] 4.1 Focused tests pass, inband and failure-feedback tests show no regression
- [x] 4.2 ruff check, ruff format, mypy --strict
- [x] 4.3 Doc links, reachability ratchet, lint gates

## 5. Live verification

- [ ] (post-merge) 5.1 Install merged main and confirm hourly acceptance `serve` check passes on `antigravity/gemini-3.5-flash-lite`
