## ADDED Requirements

### Requirement: Fixture demo never writes live proof

The offline `verdict demo` scenario SHALL NOT write session evidence, passports, health, attempt
records or the verified-run index into the user's state directory. It SHALL keep any such state
inside its own temporary workspace.

#### Scenario: Demo with an isolated state directory

- **WHEN** `verdict demo` runs with `HOME` and `VERDICT_HOME` set to empty directories
- **THEN** both directories contain no files afterwards
- **TEST** `tests/test_first_run_state_isolation.py::test_fixture_demo_writes_nothing_admission_or_proof_reads`

### Requirement: State defaults follow VERDICT_HOME

Every default path under the Verdict state directory SHALL be resolved through
`verdict.paths.verdict_home()`, so `VERDICT_HOME` relocates it. An explicit path or a
store-specific environment variable SHALL still win.

#### Scenario: Session ledger default

- **WHEN** `VERDICT_HOME` is set and `VERDICT_SESSION_EVIDENCE` is unset
- **THEN** the default session ledger is `$VERDICT_HOME/session-evidence.jsonl`
- **TEST** `tests/test_first_run_state_isolation.py::test_session_ledger_default_follows_verdict_home`

#### Scenario: No module builds a state path from the user's home

- **WHEN** the package source is scanned
- **THEN** no module builds `Path.home() / ".verdict"`
- **TEST** `tests/test_first_run_state_isolation.py::test_no_module_builds_state_path_from_path_home`

### Requirement: Decision log is written under the state directory

The default routing decision log SHALL be `$VERDICT_HOME/verdict-decisions.jsonl`, never the
working directory. `LLMGATE_LOG_PATH` and an explicit `log_path` SHALL override it.

#### Scenario: Route from a repository

- **WHEN** `verdict route --allow-offline` runs in a directory with `VERDICT_HOME` set
- **THEN** the directory gains no file and the log is in `VERDICT_HOME`
- **TEST** `tests/test_first_run_state_isolation.py::test_route_decision_log_goes_to_state_dir_not_cwd`

#### Scenario: Override

- **WHEN** `LLMGATE_LOG_PATH` is set
- **THEN** the bootstrap log path is that value
- **TEST** `tests/test_first_run_state_isolation.py::test_decision_log_default_and_override`

#### Scenario: Legacy log in the working directory

- **WHEN** a reader (stats, suggest, cost-report, ui) is given no path, `LLMGATE_LOG_PATH` is unset, and both `$VERDICT_HOME/verdict-decisions.jsonl` and `./verdict-decisions.jsonl` exist
- **THEN** it reads the state-directory log, prints one notice on stderr per process naming the legacy log and how to read it, and prints nothing extra on stdout
- **TEST** `tests/test_first_run_state_isolation.py::test_both_logs_reads_state_dir_and_notices_once_on_stderr`

#### Scenario: No hidden legacy log

- **WHEN** only the legacy log exists, a path is given, or neither log exists
- **THEN** no notice is printed, and with only the legacy log present it is read
- **TEST** `tests/test_first_run_state_isolation.py::test_no_notice_without_a_hidden_legacy_log`

#### Scenario: Runtime state directory

- **WHEN** `VERDICT_HOME` is set and `VERDICT_RUNTIME_STATE_DIR` is unset
- **THEN** the default runtime state directory is `$VERDICT_HOME/runtime`
- **TEST** `tests/test_first_run_state_isolation.py::test_runtime_state_dir_default_follows_verdict_home`
