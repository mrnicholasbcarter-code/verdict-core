## ADDED Requirements

### Requirement: Three-arm research pack bench graded by verified passes

The bench SHALL run arm A (today's retrieval-only pack), arm B (free researchers plus frontier synthesis) and arm C (arm A padded to arm B's pack size) on the same tasks, with the same worker route, worker budget, tools and verification command. Each arm SHALL be graded by whether the task's verification command passes on the worker's first attempt, not by a judged score.

#### Scenario: Only the pack differs

- **WHEN** the bench runs one task in all three arms
- **THEN** the worker route, budget, tools and verification command are identical, and only the pack differs

### Requirement: Researchers are free, read-only and bounded

Arm B's researchers SHALL run on `FREE` routes admitted with tool proof, with read-only tools only, and within a recorded cap on calls and tokens. A finding without a primary-source citation SHALL be dropped before synthesis.

#### Scenario: Researcher asks for a write tool

- **WHEN** a researcher requests a tool that writes
- **THEN** the bench refuses the call and records the refusal

### Requirement: Synthesis checks every citation

The synthesis SHALL read each cited primary source and SHALL drop a finding whose source does not support it. Every dropped finding SHALL be counted as a citation error in the report.

#### Scenario: Mis-cited number

- **WHEN** a researcher's finding cites a file that does not contain the claimed fact
- **THEN** the synthesis drops the finding and the task's citation-error count increases by one

### Requirement: A stop rule exists before the run

The bench SHALL refuse to run without a stop-rule file, and the report SHALL embed the rule's hash.

#### Scenario: No stop rule

- **WHEN** the bench is started without `--stop-rule`
- **THEN** it exits without making any model call

### Requirement: Report in mission units

For each arm the report SHALL give first-attempt pass rate, frontier tokens per accepted node including research and synthesis, free tokens, citation errors caught and the repeated-research share, with per-task detail.

#### Scenario: Report after a run

- **WHEN** a bench run completes
- **THEN** the report lists all five metrics for arms A, B and C

### Requirement: The bench changes no runtime behaviour

The bench SHALL NOT change how `verdict orchestrate` builds worker packs.

#### Scenario: Orchestrate after the bench lands

- **WHEN** `verdict orchestrate` builds a worker pack
- **THEN** it uses the existing retrieval-only pack
