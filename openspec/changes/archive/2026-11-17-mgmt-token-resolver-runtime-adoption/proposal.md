# Subagent selection and the worker runtime use the management-token resolver

## Why
PR #959 (`bod219-management-token-resolver`) moved `verdict serve` and `verdict doctor`
onto `credentials_store.resolve_management_token`: environment first, a blank value
ignored, then the same-named credential-store field. Two runtime paths still read
`OMNIROUTE_MANAGEMENT_TOKEN` from the environment directly:
`SubagentModelSelector.from_environment` (`verdict/subagent_models.py`) and
`worker_runtime.cli_run` (`verdict/worker_runtime.py`).

Neither runs through the CLI entry. `scripts/prime_supervisor.py` calls the subagent
selector, and the Prime runtime bridge starts `python -m verdict.worker_runtime`. So
`CredentialsStore.load_into_env` never copies a stored token into their environment.
A token kept only in the credential store was invisible to them, and they read only
OmniRoute's `health` source. A blank exported value was passed through as the token.
In the subagent selector that also turned on the `connections` source with no usable
token, and the transport refused to build.

Separately, the doctor "Credentials" list (and `verdict credentials list`) showed
`env` for a value that `load_into_env` had copied from the store. #959 records which
names it copied, so the listing can name the real source.

## What Changes
- `SubagentModelSelector.from_environment` and `worker_runtime.cli_run` call
  `resolve_management_token()`, with the same precedence and blank handling as serve.
  The `connections` source is enabled only when a token resolves. A group- or
  world-readable store is ignored, as the resolver already does, and the path reads
  `health` only.
- `get_credential_source` reports `store`, not `env`, when the current environment
  value equals the value `load_into_env` copied from the store in its latest load. It
  uses the existing injection record (a digest of each copy), never a comparison with
  the store's contents. An export made before the load, even one equal to the stored
  value, is still `env`. This changes listing output for a stored value read through
  the CLI entry: it now reads `store`.
- Limitation, as in #959: the record cannot observe changes after the load. A value
  that the process changes away and then sets back to the exact copied string reads as
  `store`. No mutation tracking is added.

## Capabilities
### New Capabilities
None.

### Modified Capabilities
- `management-token-resolution`: adds the subagent selector and the worker runtime to
  the paths that resolve the token through the one resolver, and makes the credential
  listing report recorded provenance. The capability is introduced by the in-flight
  `bod219-management-token-resolver` change and is not yet in `openspec/specs/`, so
  this delta adds requirements.

## Impact
- `verdict/subagent_models.py` (`SubagentModelSelector.from_environment`).
- `verdict/worker_runtime.py` (`cli_run`).
- `verdict/credentials_store.py` (`get_credential_source` only).
- Callers of `get_credential_source` that see the new `store` value: the doctor
  Credentials section (`verdict/doctor_diagnostics.py`), `verdict credentials list`
  (`verdict/actions/registry.py`), and the setup credentials checks
  (`verdict/actions/extra.py`, `verdict/cli.py`). The setup checks test only for
  `missing`, so their behaviour is unchanged.
- `docs/CONFIGURATION.md`: the `OMNIROUTE_MANAGEMENT_TOKEN` row states the precedence.
- No other direct `OMNIROUTE_MANAGEMENT_TOKEN` read remains under `verdict/`.
  `verdict/identify.py` already uses the resolver.
- ADR disposition: no new decision; this reuses the existing resolver and record.
- Not claimed: live verification on the operator host, or DoD acceptance from merged
  main.
