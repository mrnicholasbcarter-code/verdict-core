# BOD-416 slice: route catalog discovery uses the key file

## Why
`verdict route` no longer reports `credential_env_missing` when the gateway key is only in
the owner-only `omniroute-api-key` file. One step still ignored that file: catalog
discovery (`verdict/discovery.py` `fetch_models`) read only the provider's `api_key_env`
from the process environment. With `OMNIROUTE_API_KEY` unset, the `GET /v1/models` request
went out with no key. An authenticated gateway answered 401, discovery returned an empty
catalog, and when no admit snapshot decided the route, it fell back to the primary model.
With the same key exported, the route picked from the catalog. The active
`verdict-core-v1-hardening` contract already says canonical credentials bind discovery and
transport callers identically.

## What Changes
- When a provider declares a gateway key env name (`VERDICT_OMNIROUTE_API_KEY` or
  `OMNIROUTE_API_KEY`) and that name is unset, discovery takes the key from
  `credentials_store.resolve_gateway_key`, the existing single resolver. Its precedence is
  environment, `credentials.env`, the owner-only key file, then Prime `auth.json`.
- An explicit `api_key` or an exported `api_key_env` value still wins. The precedence is
  unchanged.
- Providers that declare no env name, or a different one, never receive the gateway key.

## Capabilities
### New Capabilities
- `route-discovery-gateway-key`: catalog discovery resolves a declared gateway key through
  the shared resolver.

## Impact
- `verdict/discovery.py` (`fetch_models`): one fallback branch, no new resolver.
- Tests: `tests/test_bod416_route_key_file_fallback.py`.
- ADR disposition: no new decision. This applies the existing single-resolver precedence to
  one more caller.
- Not changed: the built-in default provider set (no config file and no `OMNIROUTE_BASE_URL`)
  declares no `api_key_env`, so its discovery stays keyless for both an exported key and the
  key file.
- Not claimed: the DoD live acceptance from merged main. That stays with BOD-416.
