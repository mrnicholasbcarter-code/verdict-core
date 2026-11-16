# BOD-416 slice: doctor verifies Verdict's OmniRoute key is DB-backed

## Why
On 2026-11-04 `verdict route` failed closed because OmniRoute answered 401 on
`/api/v1/me/status`. Verdict's own key had also been set as `OMNIROUTE_API_KEY` in
OmniRoute's service environment, so OmniRoute treated it as its synthetic env key
(manage scope, no per-key policy), which `me/status` rejects. Nothing in `verdict doctor`
detected this; the operator found it by reading gateway source.

## What Changes
- `verdict doctor` resolves the key with the one resolver (`credentials_store.resolve_api_key`)
  and calls `GET /api/v1/me/status` once. 200 with `apiKey.id` is OK; 401/403 or a 200
  without key metadata is a doctor issue naming the env-key cause and remedy; 404 (gateway
  without per-key status) adds nothing; a timeout or transport error is a warning, never OK.
- The key value is never printed; only the key's name appears.

## Capabilities
### New Capabilities
- `doctor-gateway-key`: doctor preflight for a DB-backed gateway key.

## Impact
- `verdict/doctor_diagnostics.py` (`_doctor_gateway_key`, one call in the collector).
- ADR disposition: no new decision; this checks the existing single credential resolver.
- Observed: `me/status` took 16-30 s on the operator host, so the request allows 30 s.
- Not claimed: DoD live acceptance from merged main; that stays with BOD-416.
