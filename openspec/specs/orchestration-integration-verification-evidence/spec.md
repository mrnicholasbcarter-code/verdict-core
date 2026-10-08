# orchestration-integration-verification-evidence Specification

## Purpose
Make integration verification events record both the declared command and the resolved argv actually executed, matching worker evidence without changing historical receipt verification.

## Requirements

### Requirement: Integration verify events SHALL record original and executed argv

When an integration node has a verification command, `DagRuntime._integrate_node` SHALL execute the argv returned by the existing shared resolver and emit `command = shlex.join(original_argv)` and `executed_command = shlex.join(resolved_argv)` in its verify event. Both fields SHALL appear on successful and failed command exits. Existing `ok`, `exit_code`, output `tail`, node identity, and optional `resolved_argv0` evidence SHALL remain unchanged. No new shell evaluation or resolver behavior SHALL be introduced.

#### Scenario: Resolved interpreter is visible
- **WHEN** an integration verification original argv is `("python", "-m", "pytest", "-q", "tests/check with spaces.py")` and the resolver substitutes a concrete interpreter path
- **THEN** the runner receives the resolved argv, `command` is `shlex.join` of the original tuple, and `executed_command` is `shlex.join` of the resolved tuple
- **AND** `resolved_argv0` remains present when supplied by the resolver

#### Scenario: Unchanged argv still records both fields
- **WHEN** the shared resolver leaves an integration command unchanged
- **THEN** the verify event records both command fields and their values are equal to `shlex.join` of that argv

#### Scenario: Arguments with spaces or shell-sensitive characters are auditable
- **WHEN** original or resolved argv contains spaces, quotes, or shell metacharacters inside an argument
- **THEN** each command field equals `shlex.join` of its own argv, and formatting does not change the sequence passed to the runner

#### Scenario: Failed verification retains executed evidence
- **WHEN** the integration verification runner returns a nonzero exit code
- **THEN** its verify event still includes both command fields and existing failure evidence, and the integration barrier remains failed under the current rules

#### Scenario: An integration node has no verification command
- **WHEN** the integration node has no verification argv
- **THEN** existing no-command behavior is unchanged, with no new command invocation or fabricated executed-command event

### Requirement: Historical receipt verification SHALL replay recorded evidence unchanged

Historical logs and receipts SHALL remain verifiable without `executed_command`. Replay SHALL preserve recorded command strings, including the old space-joined integration rendering, rather than resolving argv again or applying new rendering rules. The receipt schema and reconstruction rules SHALL NOT change. The new review coverage rule SHALL apply to newly interpreted OCR output, not to re-interpretation of historical raw review artifacts during receipt verification.

#### Scenario: Legacy integration verify event still verifies
- **WHEN** an unchanged retained receipt was built from a verify event containing the old space-joined `command` and no `executed_command`
- **THEN** `verify_run_receipt` succeeds by replaying those recorded events without requiring the new field or changing the command string

#### Scenario: Historical recorded PASS is not re-interpreted
- **WHEN** receipt verification replays an unchanged historical recorded review PASS whose raw OCR artifact lacks positive coverage metadata
- **THEN** verification uses the recorded review evidence and existing receipt rules rather than calling the new coverage interpreter or rejecting the receipt solely because of the new rule

#### Scenario: New executed-command evidence remains digest-bound
- **WHEN** a new integration verify event includes `executed_command` and its receipt is written then verified without log changes
- **THEN** receipt verification succeeds under the existing reconstruction rules
- **AND** editing that event after receipt creation still fails the existing event-log digest check
