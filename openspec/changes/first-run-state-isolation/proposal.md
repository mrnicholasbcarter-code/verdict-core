# first-run-state-isolation: state goes under the resolved state directory

## Why

A first-run check of the v1 candidate found three places where Verdict wrote state to the wrong
place:

- `verdict demo` is a fixture demo, but it appended `real_task` pass rows to
  `session-evidence.jsonl`, wrote healthy authorized passports to `prove-at-rest/state.json`,
  wrote attempt records, and indexed its temporary run in `verified-runs.txt`. Admission and
  proof read those stores, and the indexed run was deleted, so later commands warned about it.
- `SessionLedger` defaulted to `Path.home()/.verdict` and ignored `VERDICT_HOME` (GitHub #1136).
  Fifteen other defaults built `~/.verdict` paths the same way.
- `verdict route` wrote `verdict-decisions.jsonl` into the current directory.

## What Changes

- The offline flagship scenario points every state store at a `fixture-state` directory inside
  its own temporary workspace for the duration of the run.
- Every default `~/.verdict` path is built with `verdict.paths.verdict_home()`.
- The default decision log is `$VERDICT_HOME/verdict-decisions.jsonl`. `LLMGATE_LOG_PATH`, the
  config file `log_path` and `--log_path` still override it. Readers fall back to a legacy
  `./verdict-decisions.jsonl` only when the state-directory log does not exist. When both exist,
  they print one stderr notice naming the legacy log and how to read it.
- `DEFAULT_RUNTIME_STATE_DIR` (`~/.verdict/runtime`, fixed at import, ignored `VERDICT_HOME`) is
  replaced by `default_runtime_state_dir()`. Nothing outside its own re-exports read the constant.

## Safety

- No admission or routing change. The demo stops writing fixture data where admission and proof
  read it, which only removes false evidence.
- An operator with `VERDICT_HOME` unset sees the same paths as before, except the decision log,
  which moves from the working directory to `~/.verdict/`.

## Capabilities

### New Capabilities
- `state-isolation`: where Verdict writes state, and that fixture runs never write live proof.

## Impact

- `verdict/paths.py`, `verdict/orchestration/demo_scenario.py`, the decision log writers and
  readers, and the modules listed in the pull request.
