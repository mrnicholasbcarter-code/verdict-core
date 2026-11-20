# Spec Delta

## Purpose

Fixture and demo runs SHALL NOT write any rows to the user's live
`session-evidence.jsonl` or passport store (`prove-at-rest/state.json`).
Every script that runs the orchestration pipeline SHALL redirect all state
stores to a scoped temporary directory for the duration of the run.

## ADDED Requirements

### Requirement: demo_orchestrate scopes all state stores

`scripts/demo_orchestrate.py` SHALL call `scoped_state_env()` before invoking
`run_golden_path`, directing the session ledger, health cache, and passport store
to a fixture-only subdirectory within its scratch root. The user's VERDICT_HOME
stores SHALL be byte-identical before and after any `demo_orchestrate` run.

#### Scenario: demo_orchestrate leaves session evidence unchanged

- **GIVEN** a session-evidence.jsonl file with known content under VERDICT_HOME
- **WHEN** `python scripts/demo_orchestrate.py --out <tmp>` completes
- **THEN** the session-evidence.jsonl file is byte-identical to its pre-run content

#### Scenario: demo_orchestrate leaves passport store unchanged

- **GIVEN** a prove-at-rest/state.json file with known content under VERDICT_HOME
- **WHEN** `python scripts/demo_orchestrate.py --out <tmp>` completes
- **THEN** the prove-at-rest/state.json file is byte-identical to its pre-run content

### Requirement: prime_supervisor resolves passport store from VERDICT_HOME

`scripts/prime_supervisor.py:_build_intelligence_service_from_config` SHALL use
`verdict_home()` to locate `prove-at-rest/state.json` and `model-metadata.json`,
not `Path.home() / ".verdict"`. When `VERDICT_HOME` is set, the supervisor SHALL
read passport and metadata state from that directory.

#### Scenario: prime_supervisor passport store follows VERDICT_HOME

- **GIVEN** `VERDICT_HOME=/custom/state` and a passport store at
  `/custom/state/prove-at-rest/state.json` (not at `~/.verdict/...`)
- **WHEN** `_build_intelligence_service_from_config` resolves the passport store path
- **THEN** `passport_store_path` resolves under `/custom/state/`, not under `~/.verdict/`

### Requirement: no script builds a state path from Path.home()/.verdict

No Python file under `verdict/` or `scripts/` SHALL construct a state-store path
from `Path.home() / ".verdict"` without first checking `VERDICT_HOME` via
`verdict_home()`. This is verified by the extended `test_no_module_builds_state_path_from_path_home`
scan which now covers both directories.

#### Scenario: scripts directory path scan finds no hardcoded home

- **WHEN** the `scripts/` directory is scanned for `Path.home() / ".verdict"`
  patterns that bypass `verdict_home()`
- **THEN** no offending lines are found (every state-store path goes through
  `verdict_home()`)

## UNCHANGED Requirements

The existing isolation contract for the `verdict demo` command (via
`demo_scenario.py`) is unchanged. `scoped_state_env()` semantics are unchanged.
`verdict_home()` semantics are unchanged.
