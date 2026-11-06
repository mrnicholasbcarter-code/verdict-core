## ADDED Requirements

### Requirement: A credential name in a diagnostic line is never followed by a colon or an equals sign
`BootstrapDiagnostic.describe()` MUST render
`<code> [<class>] field=<field>[ source=<source>] - <detail>; remediation: <remediation>`.
The field label and the optional source MUST be followed by ` - `, never by `:` or `=`.
Every bootstrap diagnostic line Verdict prints MUST be rendered by `describe()`. This
covers the `verdict route` and library gate stderr notes, the `verdict serve` startup
lines, `describe_bootstrap_failure`, `BootstrapError` messages and the Prime
supervisor's launch detail. The `verdict serve` startup line keeps its `verdict serve: `
prefix and carries no source. A line that names a credential and holds no credential
value MUST be returned unchanged by `verdict.security.redact_text`, so
`scripts/check_dod.py` MUST accept a raw output that holds it.

#### Scenario: Missing gateway key note
- **WHEN** the gateway provider declares `api_key_env: OMNIROUTE_API_KEY` and the name is
  unset in the environment and in the credential store
- **THEN** the CLI prints exactly
  `verdict: credential_env_missing [configuration] field=OMNIROUTE_API_KEY - provider 'omniroute' declares api_key_env OMNIROUTE_API_KEY but it is unset in the environment and in the credential store; remediation: export OMNIROUTE_API_KEY, or run 'verdict credentials set OMNIROUTE_API_KEY'`
- **AND** `redact_text` returns that line unchanged
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_credential_env_missing_note_renders_without_a_name_colon_label`,
  `::test_every_bootstrap_note_line_survives_the_redactor`,
  `::test_route_stderr_note_survives_the_redactor`

#### Scenario: Serve startup lines
- **WHEN** `verdict serve` starts with a gateway URL, no gateway key and no management
  token
- **THEN** it prints a `[configuration]` line for `field=OMNIROUTE_API_KEY` and one for
  `field=OMNIROUTE_MANAGEMENT_TOKEN`, each starting with `verdict serve: `, and
  `redact_text` returns every `[configuration]` line unchanged
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_serve_startup_lines_survive_the_redactor`

#### Scenario: DoD evidence holding the note
- **WHEN** a DoD raw-output artifact holds the bootstrap notes, including
  `credential_env_missing`, and nothing else that matches a secret
- **THEN** `scripts/check_dod.py` reports no error for it
- **TEST** `tests/test_check_dod.py::test_verdicts_own_missing_credential_note_is_not_a_credential`

#### Scenario: Pinned separator for a defaulted field
- **WHEN** the CLI falls back to the built-in provider set
- **THEN** stderr holds `[configuration] field=providers source=default - `
- **TEST** `tests/test_provider_bootstrap_adoption.py::test_resolve_default_providers_announces_the_builtin_set`

### Requirement: Secrets written in the label syntax stay redacted
`verdict.security.redact_text` MUST keep redacting a credential value written after a
credential name or a key label. This covers `NAME: value`, `NAME=value`, a quoted value,
a token after `Bearer`, and a value that is one long lowercase word. It MUST also redact
a value that is followed by prose in the old diagnostic syntax
(`field=OMNIROUTE_API_KEY: <value> provider ...`). Changing the emitter MUST NOT change
these outputs.

#### Scenario: Realistic secret shapes
- **WHEN** `redact_text` receives, with fake values, `OMNIROUTE_API_KEY: sk-...`,
  `OMNIROUTE_API_KEY=abc123...`, `api_key: <long random token>`, a double-quoted or
  single-quoted value, `Authorization: Bearer <token>`, a bare `Bearer <token>`,
  `OMNIROUTE_API_KEY: <long lowercase word>` or `api_key: <long lowercase word>`
- **THEN** it returns the label followed by `[redacted]` and the value is gone
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_realistic_secret_shapes_stay_redacted`

#### Scenario: Value followed by prose in the old diagnostic syntax
- **WHEN** `redact_text` receives `field=OMNIROUTE_API_KEY: <long lowercase word> provider 'omniroute' ...`
  or `field=OMNIROUTE_API_KEY: sk-... provider 'omniroute'`
- **THEN** it replaces only the value with `[redacted]` and keeps the prose
- **TEST** `tests/test_diagnostic_credential_name_redaction.py::test_realistic_secret_shapes_stay_redacted`
  (`field-label-lowercase-word-then-prose`, `field-label-sk-key-then-prose`)
