# Descriptor confinement implementation plan

Goal: close BOD-133 residual filesystem reads/replay without adding a runtime or dependencies.
Spec: specs/133-descriptor-confinement/spec.md
Architecture: shared descriptor-relative regular-file boundary, consumed by prompt construction, attempt hashing and untracked replay.
Execution: parent implementation (one CPU, bounded memory), independent reviewer after proof. User explicitly authorizes autonomous design/implementation/merge; routine interactive approval prompts are superseded by that authorization. No implementation worker is dispatched or attributed to a model that did not execute it.

## Global constraints
Preserve all unrelated WIP. Fail closed on unsafe paths. No network in default tests. Preserve BOD-104 strategy authority. Synthetic security tests are not live model evidence.

## Review focus
Parent link substitution; final link replacement between validation/open; FIFO blocking; destination link overwrite; partial tracked replay on denied untracked input.

## Task 1: prompt boundary
Files: tests/test_repository_confinement.py, verdict/repository_files.py, verdict/patch_executor.py.
1. RED: create root/linked -> external directory containing sentinel.txt; build_unit_prompt owning linked/sentinel.txt must raise PatchExecutorError and never return sentinel bytes. Run focused test and retain failure.
2. GREEN: add context-managed open_repository_file(root, relative_path) using held root/component descriptors, O_DIRECTORY/O_NOFOLLOW, final O_RDONLY/O_NOFOLLOW/O_NONBLOCK and fstat S_ISREG. UnsafeRepositoryPath derives OSError; close all descriptors in finally. Reject unsupported primitives, absolute/traversal paths. Integrate prompt reader; distinguish unsafe errors from ordinary absent-file omissions.
3. Add leaf-swap, FIFO, normal nested file, absent-file regressions through RED/GREEN cycles.

## Task 2: hashing and replay boundary
Files: verdict/autodev_run.py, tests/test_repository_confinement.py.
1. RED: source symlink rejection must precede applying a valid tracked diff; destination parent link must never receive replay bytes; attempt digest must reject parent link.
2. GREEN: reuse descriptor boundary for hashing. Preflight/hold all untracked source and destination parent descriptors before applying diff. Open destination exclusively and no-follow, clean newly-created destinations on failure; never overwrite pre-existing files. Preserve existing successful replay behavior.
3. Enumerate tracked/untracked paths with Git -z and test newline/space filenames. Inspect all helper callers.

## Task 3: proof and delivery
Run focused tests, existing patch executor/autodev/work-unit suites, Ruff checks/format and strict mypy. Commit only scoped files. Independent review of exact commit; fix findings, rerun affected gates. Create validated proof/receipt/review bundles through existing prime workflow, open PR with gated helper, exact-head green CI, merge, isolated main proof, Linear comment/state readback. Full local suite may be resource-bounded; CI must supply full-suite evidence before merge.
