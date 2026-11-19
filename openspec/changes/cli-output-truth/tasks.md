# Tasks

## 1. Specification

- [x] 1.1 Write proposal.md, tasks.md and the spec delta with scenarios
- [x] 1.2 `openspec validate cli-output-truth --strict` passes

## 2. Tests first

- [x] 2.1 Tampered receipt: first line, `--json` fields and exit code; untampered output unchanged
- [x] 2.2 `route --json`: stdout parses with `json.loads` on the offline gate path and the execution-path branch; denied exits non-zero with an error object
- [x] 2.3 `orchestrate --plain` and `VERDICT_PLAIN=1` select the plain view on a pseudo-terminal

## 3. Implementation

- [x] 3.1 `run-receipt` leads with the failed verification and labels the recorded outcome unverified
- [x] 3.2 `route --json` prints one JSON document, no spinner, exit code matches the decision
- [x] 3.3 `orchestrate` passes `--plain` to the progress view console
- [x] 3.4 Update `docs/CLI_REFERENCE.md` and the run-receipt JSON golden
