## ADDED Requirements

### Requirement: The chaos rehearsal SHALL start from a private copy of the certification proof
Before the chaos rehearsal, the rehearsal script SHALL copy the health cache
that the preflight wrote and the clean rehearsal used to `health-cache.json`
in a directory that the script owns and that no other run uses. The copy SHALL
be byte-identical to the source at the moment the chaos rehearsal is about to
start, so it includes what the clean rehearsal observed and no timestamp is
rewritten. The script SHALL pass a `--state-file` in that directory to the
chaos `verdict orchestrate` command and SHALL set `VERDICT_HEALTH_CACHE` to
the copy in the chaos command's environment only. The preflight SHALL run
once. The clean rehearsal's command and environment SHALL NOT change.

#### Scenario: Chaos reads the preflight's passes
- **WHEN** the preflight has recorded agentic passes and the script starts the chaos rehearsal
- **THEN** the health cache that the chaos `verdict orchestrate` command opens holds those agentic passes

#### Scenario: One private file
- **WHEN** the chaos `verdict orchestrate` command resolves its health cache for selection, for dispatch permits and for the refresh hook
- **THEN** all three resolve to the same file, and it is not the file the preflight and the clean rehearsal use

#### Scenario: Injected faults stay in the copy
- **WHEN** the chaos rehearsal records an injected failure and its cooldown
- **THEN** the bytes of the health cache the preflight and the clean rehearsal use are unchanged

#### Scenario: Clean rehearsal is unchanged
- **WHEN** the script builds the clean rehearsal command
- **THEN** it has no `--state-file`, and its environment has the shared `VERDICT_HOME` and no `VERDICT_HEALTH_CACHE`

### Requirement: The chaos rehearsal SHALL NOT start without usable proof
The rehearsal script SHALL stop with `RehearsalConfigurationError` before the
chaos `verdict orchestrate` command runs when the shared health cache is
missing or unreadable, is not a valid health cache, or holds no agentic proof
that is inside its time-to-live at that moment. The message SHALL be fixed
text that names which of the three applies and SHALL NOT carry a route id, the
gateway address or a credential.

#### Scenario: Cache missing
- **WHEN** the shared health cache does not exist when the chaos rehearsal is about to start
- **THEN** the script raises `RehearsalConfigurationError` naming "is missing or unreadable" and no chaos command runs

#### Scenario: Cache without proof
- **WHEN** the shared health cache is valid and holds no route with fresh agentic proof
- **THEN** the script raises `RehearsalConfigurationError` naming "holds no fresh agentic proof" and no chaos command runs

### Requirement: The chaos rehearsal's private state SHALL stay out of the uploaded evidence
The directory holding the chaos state file and the copied health cache SHALL
be outside every run directory and outside the evidence directory. The script
SHALL upload only the scanned run directories.

#### Scenario: Evidence holds run files only
- **WHEN** both rehearsals complete
- **THEN** the evidence directory holds the files of the two run directories and nothing from the chaos state directory
