# Tasks

## 1. Local implementation

- [x] 1.1 Add stub-transport tests in `tests/test_relay_omniroute_anomaly_recording.py` and see them fail against `main` (7 failed, all `KeyError: 'omniroute_anomalies'`).
- [x] 1.2 Add `omniroute_anomaly_names` to `verdict/outcome_log.py` and record it from `observe_execution`, so every outcome receipt row carries it.
- [x] 1.3 Record `omniroute_anomalies` on the relay's buffered and streamed evidence outcome events in `verdict/api.py`.
- [x] 1.4 Document the field in `docs/CLI_REFERENCE.md` (outcome receipts).
- [x] 1.5 Run the focused and neighbouring suites, `ruff check`, `ruff format --check` and `mypy --strict` on the touched modules.
- [x] 1.6 Validate this change with `openspec validate relay-omniroute-anomaly-recording --strict`.
- [x] 1.7 Cross-family review round 1 (`cx/gpt-5.6-luna-high`, REQUEST_CHANGES on test coverage only): add tests for `compressed`, a positive `X-OmniRoute-Cache-Hit`, repeated safe values, and a non-200 relayed status that is kept while anomalies are recorded. All 12 tests fail on `main` with `KeyError: 'omniroute_anomalies'` and pass here.
- [x] 1.8 Cross-family review round 2 (REQUEST_CHANGES on test coverage only): pin the recording-only contract. The client gets the upstream body byte for byte, the marker headers pass through, and there is one upstream attempt. Marked and clean replies give the same status, body and attempt count at 200, 400 and 503, each run with its own health cache. Repeated markers are fail-closed end to end through the buffered and streamed relay. 14 of 17 tests fail on `main`; the 3 same-as-clean comparisons pass on `main` by design, since they guard unchanged behaviour.
- [x] 1.9 Cross-family review round 3. The first input mistakenly used a two-dot diff against a `main` that had moved (#962), and those findings were about #962's changes, not this branch. The one valid finding was that emergency-fallback marker passthrough was not asserted; that is now asserted. Rebased on `2ab6ed75`, and round 3 re-ran on the corrected three-dot diff.

## 2. Pending

- [ ] 2.1 Live: relay one request through the operator's installed `verdict serve` and read `omniroute_anomalies` on its outcome receipt and evidence.
- [ ] 2.2 Independent review after merge.
- [ ] 2.3 Operator decision: dispatch-observation and attempt-ledger treatment of a marked relayed reply, and worker-reply handling in `DirectGatewayExecutor` (see `proposal.md`).
