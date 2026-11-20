# Spec Delta: Receipt pack size and context-lift scheduled bench

## ADDED Requirements

### Requirement: A run receipt SHALL project context-pack evidence from hydrate events
When `hydrate` events are present in events.jsonl, the per-attempt record in
the run receipt SHALL include a `context_pack` object with the available fields:
`context_bytes` (integer), `prompt_tokens_estimated` (integer),
`sources_count` (integer, length of the `sources` list), and `truncated`
(boolean). The field SHALL be omitted entirely when no `hydrate` event exists
for that attempt. The events.jsonl digest binding SHALL NOT change.

#### Scenario: Receipt includes context-pack when hydrate event is present
- **WHEN** a dispatch attempt has a corresponding `hydrate` event in events.jsonl
- **THEN** the attempt row in the receipt includes `context_pack` with `context_bytes`,
  `prompt_tokens_estimated`, `sources_count` and `truncated`
- **AND** `verify_run_receipt` on that run returns no errors

#### Scenario: Receipt omits context-pack when no hydrate event
- **WHEN** a dispatch attempt has no corresponding `hydrate` event
- **THEN** the attempt row has no `context_pack` key

### Requirement: `verdict bench context-lift` SHALL measure context-pack lift on free capacity
The `bench context-lift` subcommand SHALL run `run_context_lift` on FREE capacity,
write evidence to `$VERDICT_HOME/context-lift/{stamp}.json` and `latest.json`,
and exit 0 whether the result is lift, no-lift or blocked.

#### Scenario: Bench produces evidence on success
- **WHEN** `verdict bench context-lift` runs with free capacity available
- **THEN** `$VERDICT_HOME/context-lift/latest.json` is updated with the result
- **AND** the command exits 0

#### Scenario: Bench exits 0 on blocked
- **WHEN** no free identity is available or the gateway is down
- **THEN** the command writes a blocked result and exits 0

### Requirement: A daily systemd user timer SHALL keep context-lift evidence current
`verdict setup --apply` SHALL install `verdict-context-lift.timer` and
`verdict-context-lift.service`. The timer SHALL fire once daily with a 3-hour
randomised delay and persist across reboots. The service SHALL exit 0 on blocked.
`verdict doctor` SHALL report the timer's state and repair hint.

#### Scenario: Setup installs the timer
- **WHEN** `verdict setup --apply` runs
- **THEN** `verdict-context-lift.timer` is enabled and active
- **AND** `verdict doctor` shows the context-lift bench timer as ok

#### Scenario: Doctor reports missing timer
- **WHEN** the timer unit is not installed
- **THEN** `verdict doctor` reports the context-lift bench timer as failed with a repair hint
