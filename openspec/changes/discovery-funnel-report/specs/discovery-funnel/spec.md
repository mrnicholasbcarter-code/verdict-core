## ADDED Requirements

### Requirement: Funnel stages SHALL be computed from cached evidence only

`verdict discovery funnel [--json]` SHALL read `~/.verdict/identify-latest.json`,
`~/.verdict/health-cache.json` and `~/.verdict/admission-latest.json`.  It MUST
call no live API endpoints, make no model calls, make no probes and make no writes.
After the command runs, the mtime of every file in `~/.verdict` MUST be unchanged.

#### Scenario: All sources present

- **GIVEN** `identify-latest.json`, `health-cache.json` and `admission-latest.json` are all present
- **THEN** the command prints a stage table with integer counts for listed, free_by_fact, probed, answered, tool_ok and admitted
- **AND** exits 0

#### Scenario: All sources missing

- **GIVEN** none of the cache files exist
- **THEN** every stage reports "unknown" and the command exits 0

#### Scenario: Health cache present, admission missing

- **GIVEN** `health-cache.json` is present but `admission-latest.json` is absent
- **THEN** stages listed through tool_ok report counts and admitted reports "unknown"

### Requirement: Stage counts SHALL be set-based and MUST intersect forward

Each stage's count SHALL be derived as a subset of the previous stage so that a
stage never reports more routes than the stage before it.  "Admitted" and "tool_ok"
MUST be reported independently because admission runs on the full catalog, not on
the tool_ok set.

#### Scenario: Admitted count is independent of tool_ok

- **GIVEN** admission-latest.json has an admitted set with routes that were not in the health cache tool_ok set
- **THEN** the admitted count reflects the admission file directly, not a subset of tool_ok

### Requirement: Caps SHALL be named by their module-level constant

Every cap that limits routes at a stage SHALL be named by its Python constant
(e.g. `SCREEN_MAX_ROUTES=40`, `max_probes_per_select=8`) and its value.
Numbers MUST NOT be hard-coded in the output.

#### Scenario: Cap name and value appear in plain output

- **GIVEN** `verdict discovery funnel` is run
- **THEN** plain-text output includes at least one cap name in the form `CONSTANT_NAME=value`

### Requirement: Unknown MUST NOT be zero

A stage whose evidence file is absent SHALL report `null` (JSON) or "unknown"
(plain text).  It MUST NOT report 0.

#### Scenario: Missing source reports unknown not zero

- **GIVEN** none of the cache files exist
- **THEN** every stage count in `--json` output is `null`, never `0`

### Requirement: JSON output SHALL validate against discovery-funnel.v1 schema

`verdict discovery funnel --json` SHALL emit a JSON object whose `schema` field
is `verdict.discovery.funnel/v1` and whose structure MUST match
`schemas/discovery-funnel.v1.json`.

#### Scenario: --json flag

- **GIVEN** `verdict discovery funnel --json` is run with all sources present
- **THEN** the output is valid JSON that validates against the schema
- **AND** contains the keys `schema`, `generated_at`, `stages` and `caps`
