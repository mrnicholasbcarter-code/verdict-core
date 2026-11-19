# Tasks

## 1. Tests first

- [x] 1.1 Demo leaves an isolated HOME and VERDICT_HOME empty
- [x] 1.2 SessionLedger default follows VERDICT_HOME
- [x] 1.3 `verdict route` writes no file in the working directory
- [x] 1.4 Decision log default and LLMGATE_LOG_PATH override
- [x] 1.5 Source scan: no `Path.home() / ".verdict"`

## 2. Implementation

- [x] 2.1 Demo scenario scopes state env to its workspace
- [x] 2.2 State defaults use `verdict_home()`
- [x] 2.3 Decision log default under the state directory, legacy read fallback
- [x] 2.4 Docs updated (CONFIGURATION, CLI_REFERENCE, guides)

## 3. Live checks

- [ ] (post-merge) 3.1 On installed main, `HOME=$X VERDICT_HOME=$X/.verdict verdict demo` leaves `$X` without files
