# Bootstrap diagnostics stop reading as a leaked credential

## Why
`scripts/check_dod.py` refuses a DoD raw-output artifact when
`verdict.security.redact_text(text) != text`. A real gate log held this line, which
Verdict prints when the gateway key is unset:

```
verdict: credential_env_missing [configuration] field=OMNIROUTE_API_KEY: provider 'omniroute' declares api_key_env OMNIROUTE_API_KEY but it is unset in the environment and in the credential store; remediation: export OMNIROUTE_API_KEY, or run 'verdict credentials set OMNIROUTE_API_KEY'
```

The last pattern in `verdict.security._SECRET_PATTERNS` matches a name that contains
`api_key`, `token`, `password` or `secret`, then `[=:]` with optional whitespace,
then a quoted string or a run of characters other than whitespace, `&`, `,` and `;`. It
matched `OMNIROUTE_API_KEY: provider` and rewrote the line to
`field=OMNIROUTE_API_KEY: [redacted] 'omniroute' ...`. The log holds no credential, so
the artifact was refused in error. `verdict serve` prints the same format by hand, and
its `runtime_sources_health_only` line
(`field=OMNIROUTE_MANAGEMENT_TOKEN: runtime sources: ...`) has the same problem.

The redactor is right to match that shape. `NAME: word` is how a key is written in YAML
and in environment dumps. A rule that skips a value with prose after it, or a value
that is one lowercase word, would also skip `OMNIROUTE_API_KEY: qzxkvbnwmtrplhgfdsjy`.
Telling `provider` apart from a random lowercase token needs a dictionary or an
entropy guess. So the fix is in the emitter: a credential *name* in the field position
is never followed by `:` or `=`.

## What Changes
- `BootstrapDiagnostic.describe()` renders
  `<code> [<class>] field=<field>[ source=<source>] - <detail>; remediation: <remediation>`.
  Only the separator after the field label changes, from `: ` to ` - `.
- The `verdict serve` startup loop renders each diagnostic through
  `BootstrapDiagnostic.describe()` and no longer builds the line by hand. As before, the
  serve line carries no source.
- The CLI notes from `verdict route` and the library gate, `describe_bootstrap_failure`,
  `BootstrapError` messages and the Prime supervisor's launch detail all render through
  `describe()`, so this change covers them too.
- `describe()` also passes its field, detail and remediation through `redact_text`, one
  part at a time, and keeps the documented `***:***@` userinfo marker. Under the old
  `field=NAME: detail` layout the label pattern caught a secret at the start of a
  detail by accident; this makes that a guarantee for secret shapes. A bare value with
  no label and no known prefix is still not caught. For those, a structural test
  checks that no diagnostic is built from a credential value.
- `verdict.security` is not changed.
- **Format change:** a consumer that splits these lines on `field=<name>:` must split
  on ` - `. No consumer in this repository parses them. The one test that pinned the
  old separator (`test_resolve_default_providers_announces_the_builtin_set`) now pins
  the new one.
- Not changed: lines already captured in a log keep the old format. A gate log
  captured before this change still fails `check_dod.py` and has to be captured again
  from a build with this change.

## Capabilities
### New Capabilities
- `diagnostic-credential-names`: diagnostic lines that name a credential pass the
  shared redactor unchanged, and realistic secrets in the same syntax are still
  redacted.

### Modified Capabilities
None.

## Impact
- `verdict/provider_bootstrap.py` (`BootstrapDiagnostic.describe`, private
  `_redact_free_text`).
- `verdict/api.py` (`lifespan` startup diagnostic loop).
- `tests/test_diagnostic_credential_name_redaction.py` (new),
  `tests/test_bootstrap_diagnostics_carry_no_credential_values.py` (new),
  `tests/test_check_dod.py` (one test), `tests/test_provider_bootstrap_adoption.py`
  (one pinned separator).
- Not changed: `BootstrapDiagnostic.to_dict()` still serializes the raw detail. The
  structural test shows no detail carries a credential value, but `to_dict()` itself
  does not redact.
- `docs/CONFIGURATION.md` ("Secrets in rendered output"), `CHANGELOG.md`.
- Other diagnostics checked and left alone, because the redactor does not change them:
  the harness status line `token_env: <NAME> (set: ...)`, the JSON `api_key_env` and
  `token_env` fields, the doctor issue `OMNIROUTE_MANAGEMENT_TOKEN rejected: HTTP ...`,
  and the `verdict credentials set` prompt and status row.
- ADR disposition: no new decision.
- Not claimed: live verification on the operator host, or DoD acceptance from merged
  main.
