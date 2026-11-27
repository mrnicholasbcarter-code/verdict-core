# Tasks — recovering from a serve route denial

## 1. Classification

- [ ] 1.1 `FailureIntelligence.classify` returns `route_denied` (`REROUTE`, scope `none`, cooldown 0, code in the evidence) for a 503 terminal whose error is serve's denial of its own route; a 5xx without the text, and a denial text naming another route, stay `upstream_temporary`. Verify: unit tests in `tests/test_orch_recovery.py`.
- [ ] 1.2 An attempt that failed with `route_denied` has the ledger class `other`, not `provider_5xx`. Verify: a test on `run_attempt_records`.

## 2. Run-scoped exclusion and cooldown

- [ ] 2.1 The ladder excludes a route for the run by narrowing its admitted set with reason `serve_denied:<code>`, and holds run-scoped cooldowns (route or id prefix) in memory. Verify: unit tests that a later `select` skips the route and reports the reason, that a prefix cooldown skips `claude/X` and not `cc/X`, that the cooldown expires, and that the health state file is byte-identical before and after.

## 3. Runtime

- [ ] 3.1 On `route_denied` the runtime excludes (permanent code), cools the prefix for 300 s (`provider_unhealthy`) or cools the route for 60 s (any other code), and emits the `controller` event. Verify: runtime tests with a fake executor returning the denial terminal: a second node never selects a permanently refused route; no `cooldown` event, no session-feedback record and no persisted cooldown for a denial; a real 503 still produces them.

## 4. Live check

- [ ] (post-merge) 4.1 On installed main, an unscoped `verdict orchestrate` run in which a refused route is selected loses under 60 s to each refusal and never selects a permanently refused route twice. Verify: the run's events and receipt.
