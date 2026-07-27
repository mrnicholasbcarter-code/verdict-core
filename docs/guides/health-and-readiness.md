# Health and readiness

Verdict exposes two intentionally different operational probes:

- `GET /health` is a process liveness probe. A `200` response means the HTTP
  process is serving requests; it does **not** claim that OmniRoute, Ruflo,
  RuVector, or any model provider is reachable. The response includes
  `scope: liveness` and `dependencies_checked: false` so monitoring systems do
  not have to infer the boundary from a status string.
- `GET /ready` is the dependency/readiness probe. It checks the initialized
  intelligence service and configured upstream, reports the intelligence
  readiness payload, and returns a non-2xx response when the request path is
  not ready for traffic.

Use `/health` for process supervisors and `/ready` for traffic admission. A
provider outage must not turn liveness into a false positive for readiness, and
a healthy process must not be reported as a healthy model runtime.
