# BOD-133 descriptor confinement residual

## Intent and current evidence
Make the coding-agent flagship safe against worker-controlled filesystem paths. Main 3666450 includes the original BOD-133 changes through PR570, but build_unit_prompt checks only the final symlink and then uses Path.read_text; _replay_attempt checks is_symlink then shutil.copyfile; _attempt_digest follows files. The existing security/demo baseline passed 157 tests without exercising parent links or race-resistant descriptors.

## Design decision
Extend canonical patch_executor and autodev_run with a small shared repository-file boundary. Reject symlinks at every path component, non-regular files and non-normalized paths. Open directory components relative to held directory descriptors using O_DIRECTORY/O_NOFOLLOW; open the final file with O_NOFOLLOW/O_NONBLOCK and fstat regular-file validation. No inferred safe path from resolve-then-open. Fail closed on platforms lacking these primitives. Missing owned files remain supported for creating new files, but unsafe paths raise a named error before inference.

Replay must preflight all untracked sources and destinations before applying a tracked diff. Hold verified descriptors rather than reopening source pathnames after validation. Destination writes cannot follow final/parent links or overwrite an existing untracked file. File enumeration uses Git -z and preserves spaces/newlines. Attempt hashing uses the same read boundary. This is confinement against path traversal/link substitution, not a sandbox against arbitrary processes with the user's privileges or hostile concurrent in-place mutation of an already-open inode.

Alternatives rejected: another is_symlink/resolve check still races; a new container runtime is excessive for this bounded residual and changes execution architecture.

## Acceptance/proof
AC1 Parent and leaf symlinks never enter model prompts; no transport runs on denied input.
AC2 FIFOs/non-regular files reject without blocking; missing ordinary files remain a named unreadable omission.
AC3 Untracked replay rejects unsafe sources/destinations before tracked changes, and preserves filenames with whitespace.
AC4 Attempt hashes never read through symlinks; all descriptor resources close on failure.
AC5 Existing patch ownership including rename/copy/binary headers remains enforced, with the existing regressions retained.
Proof: focused regression file plus existing patch executor/autodev/work-unit tests; Ruff/format/strict mypy; independent review; exact-head CI; isolated merged-main replay. Synthetic security fixtures are not live model evidence.

## Scope
verdict/repository_files.py (new shared primitive), verdict/patch_executor.py, verdict/autodev_run.py, tests/test_repository_confinement.py and this spec/plan/tasks. Preserve unrelated worktrees, credentials and all routing authorities. No new dependencies. One issue, owner, branch, worktree.
