# Gateway key resolver parity: direct-gateway executor and credential test

## Why
Verdict has one gateway key resolver, `credentials_store.resolve_gateway_key`. Its precedence
is environment, `credentials.env`, the owner-only `omniroute-api-key` file, then Prime
`auth.json`. A reconciliation of stranded commits against main found two consumers that
still read the environment only. On a host whose key is only in the key file (the shape a
systemd user unit runs with):

- `DirectGatewayExecutor` built its own environment chain, so it sent no `Authorization`
  header while every other transport authenticated. `verdict orchestrate` passes an empty
  key on purpose so the executor applies precedence, so the gap was in the executor.
- `verdict credentials test OMNIROUTE_API_KEY` answered `missing` and never ran the live
  check, while `verdict credentials list` reported the same key as present from `key_file`.

## What Changes
- `DirectGatewayExecutor` takes its key from an explicit argument, then the shared resolver,
  then `OPENAI_API_KEY` as the existing last resort.
- `credentials.test` falls through to the shared resolver when a gateway key name is unset
  in the environment and the credential store.
- An explicit argument or an exported key still wins. Precedence is unchanged.
- Credentials that are not gateway key names never receive the gateway key.

## Capabilities
### New Capabilities
- `gateway-key-resolver-parity`: the direct-gateway executor and the credential live check
  resolve the gateway key through the shared resolver.

## Impact
- `verdict/orchestration/executors.py` (`DirectGatewayExecutor.__init__`), one expression.
- `verdict/actions/extra.py` (`_action_credentials_test`), one fallback branch.
- Tests: `tests/test_direct_gateway_executor.py`, `tests/test_credentials_test_action.py`,
  and the former placeholder in `tests/test_actions_no_secret_leak.py`.
- ADR disposition: no new decision. This applies the existing single-resolver precedence to
  two more callers.
- Not changed: the retired `user_services` registry, the retired prober timer and the
  retired cache-age rule stay retired; per-service installers and identify maintenance
  already cover that behaviour.
