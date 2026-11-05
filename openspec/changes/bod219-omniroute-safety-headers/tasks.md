# Tasks

## 1. Local implementation

- [x] 1.1 Add stub-transport tests in `tests/test_bod219_omniroute_safety_headers.py` and see them fail against the `main` call sites (17 failed, 15 passed with only the helper present).
- [x] 1.2 Add the shared helper in `verdict/http_safety.py`: evidence opt-out headers with a fresh `X-Request-Id`, named response anomalies and the proof-voiding reason.
- [x] 1.3 Send the headers from `openai_probe_transport`, prove-at-rest chat/tool and agentic transports, `openai_health_probe`, the acceptance tool check, the census needle probe, `DirectGatewayExecutor` and the relay upstream proxy.
- [x] 1.4 Fail closed on a cache hit or emergency fallback in the probe runner, prove-at-rest (no-write), agentic recording, acceptance tool check, eligibility health probe and census needle probe.
- [x] 1.5 Run the focused and neighbouring suites, `ruff check`, `ruff format --check` and `mypy verdict --strict`.
- [x] 1.6 Validate this change with `openspec validate bod219-omniroute-safety-headers --strict`.

Not done here, and not ticked: live verification on the operator's OmniRoute
gateway, independent review, and the follow-ups listed in `proposal.md`
(relay anomaly recording in `verdict/api.py` and the uncovered request paths).
