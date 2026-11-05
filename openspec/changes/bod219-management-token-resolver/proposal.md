# BOD-219 slice: serve and doctor use the one management-token resolver

## Why
`verdict serve` read the OmniRoute management token with
`os.getenv("OMNIROUTE_MANAGEMENT_TOKEN")` in two places: the availability transport
builder and the `runtime_sources_health_only` startup diagnostic. BOD-345 added
`credentials_store.resolve_management_token` (environment first, blank ignored, then the
same-named credential-store field), and passive identify uses it. Serve did not.

The CLI entry copies stored values into the environment only for names that are not set
at all (`CredentialsStore.load_into_env`). An exported but blank
`OMNIROUTE_MANAGEMENT_TOKEN`, or the app imported without the CLI entry, left serve with
no token. A blank exported value also enabled the `connections` source with no token,
and the transport then refused to build. Without a token, serve reads only OmniRoute's
`health` source: providers outside OmniRoute's monitored set read as unknown and the
free-tier admission intersection fails closed. So unattended units such as the hourly
acceptance job, whose serve check spawns `verdict serve`, needed a hand-made
EnvironmentFile. Nothing in `verdict doctor` said whether the token serve would use
exists, where it comes from, or whether OmniRoute accepts it.

## What Changes
- `verdict serve` resolves the management token with `resolve_management_token()` in
  the availability transport builder and in the startup diagnostic. The
  `runtime_sources_health_only` diagnostic appears only when the resolver returns
  nothing.
- `verdict doctor` adds a "Management token" row from the same resolver:
  - absent: info (`note`), "runtime sources: health only", with the repair command;
  - present: one bounded read-only `GET /api/resilience/connections` (the management
    route serve's transport reads) on the configured gateway origin via
    `open_no_redirect`, with the transport's 5 s timeout and its 16 MiB response
    ceiling, and no retry;
  - 200 with a JSON body: OK; 401 (rejected) or 403 (forbidden): a doctor issue,
    because serve's runtime read then fails and routing fails closed; any other
    status, timeout, transport error or body: a warning, never OK.
- Every row names the token's source (`environment` or `credential_store`), never its
  value. The source is recorded, not inferred from values.
  `CredentialsStore.load_into_env` records each name it copies into the process
  environment, with a SHA-256 digest of the value and never the value itself.
  `credentials_store.resolve_management_token_source` returns `credential_store` for
  a value that was copied in and not changed since, and `environment` for any other
  non-blank export, even one equal to the stored value. The repair is `verdict credentials set OMNIROUTE_MANAGEMENT_TOKEN --stdin`, and
  the row says that OmniRoute accepts an `oma_` access token or a manage-scope API key.
  For an exported token, the issue also says that the export overrides the store.
- The text renderer's repair hint for a rejected or forbidden management token is that
  exact command, not the generic gateway hint.

## Capabilities
### New Capabilities
- `management-token-resolution`: serve and doctor read the management token through
  the one resolver, and doctor validates it.

## Impact
- `verdict/api.py` (`_build_availability_cache`, `server_bootstrap_diagnostics`).
- `verdict/doctor_diagnostics.py` (`_resolved_management_token`,
  `_doctor_management_token`, one call in the collector).
- `verdict/doctor_presentation.py` (`repair_command`, one branch).
- `verdict/credentials_store.py`: `load_into_env` records what it injects, and adds
  `injected_from_store` and `resolve_management_token_source`.
  `resolve_management_token` is unchanged, so BOD-345 passive identify is unchanged
  and `verdict/identify.py` is not edited.
- Not changed: `get_credential_source`, behind the doctor "Credentials" list and
  `verdict credentials list`, still reports `env` for a value `load_into_env` copied
  in. Changing it would change existing listing output.
- Not in this slice: `verdict/subagent_models.py` and `verdict/worker_runtime.py` still
  read the environment variable directly. They are outside this slice's file scope.
- ADR disposition: no new decision; this reuses the existing resolver.
- Not claimed: live verification on the operator host, or DoD acceptance from merged
  main. Those stay with BOD-219.
