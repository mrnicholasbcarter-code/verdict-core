# Tasks: BOD-12 Contract Parity

- [x] T001 Audit current four contract kinds and their Python/TypeScript representations against ADR-025.
- [x] T002 Add shared fixture(s) for routing decision, execution envelope, provider receipt, and proof reference only where missing.
- [x] T003 Add/extend Python round-trip and unknown-field tests over shared fixtures.
- [x] T004 Add/extend TypeScript Zod schemas/types and tests over the same fixtures.
- [x] T005 Prove JSON schema copies are byte-identical and default/null semantics agree.
- [x] T006 Run STATIC, UNIT, INTEGRATION, and ACCEPTANCE-PROOF commands.
- [x] T007 Return changed files/commit, exact commands/exit codes/test counts, acceptance status, proof evidence, and uncertainty. Open the PR only after the proof and review bundle validates; do not mark Linear Done until merged-main verification.

## Constraints

- Sole writer worktree: `/home/nick/dev/.worktrees/bod-12-contract-parity`
- Do not edit `/home/nick/dev/verdict-core` or any other worktree.
- No unrelated schema cleanup and no breaking version bump.
