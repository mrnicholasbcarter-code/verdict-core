# Design — a node's commit keeps the git-ignored files the node owns

## Context

Each node attempt runs in its own git worktree. `DagRuntime._validate` asks `Git.changed_files` for the attempt's changes, checks ownership, runs the node's verification command there, and `commit_all` then commits what is staged. Dependent nodes start from that commit. Verification sees every file in the worktree; the commit saw only what `git add -A` stages, which excludes git-ignored paths. Owned paths under a byproduct directory were already force-added by literal path.

## Goals / Non-Goals

Goals:

- The commit of a VALIDATED node contains every owned file that verification could see, including files a `.gitignore` pattern matches.
- A file that must not or cannot be committed is refused openly, and the attempt fails with a named reason.
- The run's events say which paths were force-staged.

Non-Goals:

- Ignored files the node does not own. A worker's own tooling leaves such files (build output, logs), so reporting them needs its own research; tracked on GitHub issue #1372.
- Re-running verification in a clean checkout of the node's commit (the operator declined it for now: it runs verification twice per node).
- Making the reviewer see force-staged files (GitHub issue #1381).

## Decisions

1. **Force-stage by literal path, owned paths only.** The listing is limited to the node's owned files and directories, and each path is added with a `:(literal)` pathspec. A directory is never force-added wholesale.
2. **Byproducts are excluded in the git call.** Every `BYPRODUCT_EXCLUDES` entry becomes an exclude pathspec, so byproduct files never reach the listing. The same filter is applied again to each listed path, so a file matching a byproduct file pattern (`*.pyc`, `.coverage`) is not staged even when it is named explicitly in the owned files. Unchanged from before: an owned path that itself lies under a byproduct directory is force-added by its literal path in an earlier step.
3. **The listing is refused when it may be truncated.** The command runner keeps the last 4,000 characters of output; a listing of that length or more raises instead of being used.
4. **Content check before staging (operator decision, 2026-11-26).** A file is staged only when redaction would not change its content; a file that cannot be read is refused as well. `.gitignore` also hides secret files, and `verdict deliver` pushes node commits.
5. **A refusal fails the attempt.** Refused paths are reported with a reason (`credential_shaped`, `unreadable`, `symlink`, `not_a_regular_file`) on the existing `ownership` barrier event, not on a new barrier name: the receipt keeps the last barrier event per name and requires each to be ok, so a failure-only barrier would stay failed after a good retry. The result string starts with `verification_failed:` so the worker receives it as correction feedback; `ownership_violation` would tell the worker it had changed files outside its ownership.
6. **Per-worktree report.** One `Git` object serves concurrently running nodes, so the report is keyed by the worktree directory.

## Risks / Trade-offs

- The refusal covers implement nodes only; another node kind with owned files still skips such a file without failing.
- A legitimate fixture that contains credential-shaped text is refused; the worker has to build that data at test run time.
- An owned ignored file whose name is not valid UTF-8 is reported under a replaced name and refused as `not_a_regular_file`.
- Symlinks and non-regular files are refused, which goes beyond the operator's decision on credential-shaped and unreadable files; it follows the same principle and is flagged on the issue.

## Security / trust implications

No ignored file is committed without the content check, and the check is the whole-file rule salvage already uses. Events and worker feedback carry paths and reason codes only, never file content. Byproduct directories (`venv/`, `.verdict/` and the like) are excluded before any path is considered.

## Routing / context / memory implications

None for route selection, cooldowns or memory. A refusal is a `verification_failed` judged failure and counts toward capability escalation like any other.

## Migration / rollback

No stored state changes; the event fields are optional and additive. Reverting the commits restores `git add -A` staging.

## ADR impact

None.
