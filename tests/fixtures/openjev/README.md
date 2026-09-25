# OpenJev Test Fixtures (BOD-199)

Hermetic test fixtures for OpenJevSystemOneProvider. All tests use injectable transport; no network calls.

- `confident.json`: High confidence, frontier_worthy=0.95
- `uncertain.json`: Low confidence, frontier_worthy=0.48
- `malformed.json`: Invalid JSON
- `schema_violation.json`: Missing required field
- `timeout`: Transport raises TimeoutError
- `quota_429.json`: HTTP 429 with quota exhausted code
- `rate_limit_429.json`: HTTP 429 with Retry-After header
- `overload_529.json`: HTTP 529 provider overload
- `key_missing`: OPENJEV_API_KEY unset
