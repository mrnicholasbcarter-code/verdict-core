# Tasks

## 1. Specification
- [x] 1.1 Proposal, tasks and the spec delta with scenarios
- [x] 1.2 `openspec validate health-probe-missing-content --strict` passes

## 2. Tests first
- [x] 2.1 Fixture of the captured shape, redacted: `tests/fixtures/health_probe/missing-content-length.json`
- [x] 2.2 Captured shape healthy under `missing_content`; `stop`, refusal, tool calls, function call, error, zero or two choices, non-assistant role, unverified identity, unreadable token count and unreadable reasoning stay failed; empty-content and null-content flags unchanged: `tests/test_health_probe_missing_content.py`

## 3. Implementation
- [x] 3.1 `_empty_but_alive_flags` and `_missing_content_is_readable` in `verdict/subagent_selection.py`
- [x] 3.2 `docs/guides/health-cache.md` describes what the liveness probe accepts

## 4. Live check
- [x] 4.1 One probe of `cerebras/gpt-oss-120b` through the local gateway from this branch: healthy, flag `missing_content` (2026-11-19 11:07Z)

- [ ] (post-merge) A certification rehearsal on merged main admits `cerebras/gpt-oss-120b` at `HEALTHY` when its probe reply has no content key
