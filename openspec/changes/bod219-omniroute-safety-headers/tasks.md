# Tasks

## 1. Local implementation

- [x] 1.1 Add stub-transport tests in `tests/test_bod219_omniroute_safety_headers.py` and see them fail against the `main` call sites (17 failed, 15 passed with only the helper present).
- [x] 1.2 Add the shared helper in `verdict/http_safety.py`: evidence opt-out headers with a fresh `X-Request-Id`, named response anomalies and the proof-voiding reason.
- [x] 1.3 Send the headers from `openai_probe_transport`, prove-at-rest chat/tool and agentic transports, `openai_health_probe`, the acceptance tool check, the census needle probe, `DirectGatewayExecutor` and the relay upstream proxy.
- [x] 1.4 Fail closed on a cache hit or emergency fallback in the probe runner, prove-at-rest (no-write), agentic recording, acceptance tool check, eligibility health probe and census needle probe.
- [x] 1.5 Run the focused and neighbouring suites, `ruff check`, `ruff format --check` and `mypy verdict --strict`.
- [x] 1.6 Validate this change with `openspec validate bod219-omniroute-safety-headers --strict`.

## 2. Review fixes (cross-family review of `e05a2786`)

- [x] 2.1 Add red tests: marker values other than the exact safe value (`no`, `0`, empty, unknown text, `BYPASS`, wrong case) and repeated markers are anomalies; the identify screen does not enter the cache-write path for a fallback turn (38 failed on `e05a2786`).
- [x] 2.2 Match markers fail-closed against the exact OmniRoute 3.8.53 safe values, with the package source lines cited in `verdict/http_safety.py`.
- [x] 2.3 Decide on an untrusted turn before any persistence in `screen_route` and at the top of `_record_agentic`.
- [x] 2.4 Add real-path integration tests with a real `HealthCache`: a daemon cycle with a cached or fallback chat reply leaves earlier usable proof unchanged and writes nothing for an unproven route; an agentic run that reaches a fallback third turn writes no capability. Each has a clean-reply control, and the marked cases fail against `main` call sites.

Not done here, and not ticked: live verification on the operator's OmniRoute
gateway, independent review, and the follow-ups listed in `proposal.md`
(relay anomaly recording in `verdict/api.py` and the uncovered request paths).
