# Tasks

## 1. Probe
- [x] 1.1 Captured empty-content replies alive and flagged; zero-token flagged; no identity, wrong model, cache hit, malformed shapes stay failed: `tests/test_liveness_probe_agentic_isolation.py`
- [x] 1.2 `_empty_but_alive_flags` in `verdict/subagent_selection.py`

## 2. Isolation
- [x] 2.1 Liveness failure keeps agentic and session proof, still marks chat unhealthy; unproven route gets no invented proof: `tests/test_liveness_probe_agentic_isolation.py`
- [x] 2.2 Non-liveness callers unchanged; exempt category set not widened: `tests/test_liveness_probe_agentic_isolation.py`
- [x] 2.3 `liveness_only` in `record_dispatch_observation`, `keep_agentic` and `keep_tool_ok` in `HealthCache.record`, passed by the ladder and the worker launch probe
- [x] 2.4 `tool_ok` kept on a failed liveness observation; the entry stays unhealthy and the status report lists no worker from it: `tests/test_liveness_probe_agentic_isolation.py`

- [ ] (post-merge) After reinstall, the hourly acceptance `choose` probe leaves tool proof in `~/.verdict/health-cache.json` unchanged for routes that answer with empty content
