# Doctor gateway probes fit a slow but healthy gateway

## Why

On 2026-11-19 at 13:50Z `verdict doctor` exited 1 and reported "Gateway unreachable"
against a healthy OmniRoute: `/api/health` answered 200 in 5 ms, but the first
authenticated `/models` call took 3.7 to 4.6 s with the 1-minute load average at 13.
Both doctor gateway probes used a fixed 2 s timeout with no retry and no setting, so
doctor also left three core capabilities DEGRADED.

## What Changes

- The authenticated `/models` readiness probe that doctor uses waits 10 s by default
  (`VERDICT_DOCTOR_GATEWAY_TIMEOUT_S`) and retries once, only after a timeout.
- The unauthenticated `/api/health` check waits 3 s by default
  (`VERDICT_DOCTOR_HEALTH_TIMEOUT_S`) and retries once, only after a timeout.
- An HTTP answer of any status (401, 403, 5xx) is final and never retried. Healthy still
  needs a 2xx (`/models`) or a 200 (`/api/health`).
- A probe that times out after its retry fails with a message that names the timeout and
  the setting that raises it.
- A setting that is not a finite number in (0, 120] is ignored; the default applies.
- `http_gateway_probe` and `authenticated_gateway_probe` gain optional
  `timeout_retries` and `timeout_hint` arguments. Defaults keep the old behaviour for
  `verdict serve` and the lifecycle wait.

## Impact

`verdict/doctor_diagnostics.py`, `verdict/gateway_lifecycle.py`, `docs/CONFIGURATION.md`,
tests. No admission, eligibility, router or orchestration change. Doctor still makes no
model calls.
