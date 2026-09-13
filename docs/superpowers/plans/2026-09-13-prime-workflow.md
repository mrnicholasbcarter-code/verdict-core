# Prime workflow implementation plan

**Goal:** Implement the approved five project-owned Prime skills and bounded stall recovery.
**Architecture:** Markdown skills in Prime's actual `.prime/agent/skills` discovery directory share a versioned contract and Python validation helpers. A subprocess supervisor supplies liveness independently of model turns. Linear plans; GitHub owns code truth. Existing Spec Kit and routing capabilities remain underlying dependencies.
**Tech stack:** Prime 0.9.4, Python standard library, pytest, git, existing MCP/gh integrations.
**Spec:** User-approved five-stage workflow and lifecycle; ADR-031 below records the implementation boundaries.

## Constraints
- Exactly verdict-resume, hydrate-context, verdict-dispatch, verdict-proof, verdict-finish.
- READY -> HYDRATED -> IMPLEMENTING -> LOCAL_VALIDATION -> PROOF_COMPLETE -> REVIEW_APPROVED -> PR_OPEN -> CI_GREEN -> MERGED -> MAIN_VERIFIED -> DONE.
- No conversational-memory authority, default model aliases, duplicate writers, false proof, or unlimited retries.
- VS Code Server is optional. Sequential Thinking is advisory. MCP availability must be observed.
- Preserve existing dirty checkout; implement from origin/main in isolated worktree.

## Tasks
1. [x] Record baseline contract gaps using existing autonomous-development skill; inventory actual Prime loader, extensions, Spec Kit and MCP servers.
2. [x] Add `tests/test_prime_workflow.py`: missing files fail discovery; blocked/unknown dependencies cannot win; proof fails for stale SHA, missing categories, unexplained N/A, missing AC evidence; terminal receipt and state transitions reject missing evidence. Run `python3 -m pytest tests/test_prime_workflow.py -q` before implementation.
3. [x] Implement `scripts/prime_workflow.py` shared validators and CLI. Keep GitHub writes behind verified local proof and review; validate current clean source. Re-run focused tests.
4. [x] Add supervisor tests for process exit, no-progress despite heartbeat, timeout, bounded retry, concurrent ownership. Implement `scripts/prime_supervisor.py` with a real process group, common-git lock, durable events and bounded restarts. Fake worker tests exercise termination without live models.
5. [x] Write each of the five skills under `.prime/agent/skills/<name>/SKILL.md`, with shared `docs/guides/prime-workflow.md` contract and exact runtime-supported commands. Extend existing concepts without adding capability skills.
6. [x] Verify using Prime's installed skill loader, behavioral review scenarios, focused pytest, ruff, compile and diff checks. Document MCP audit and live-integration limitations. Provide start and recovery commands.

No commit, push, merge, or existing-session termination is required to deliver the project-local skills. Runtime evidence belongs in git's common directory, not tracked files.

## Validation result

30 focused Python tests and the Node compaction test passed. Installed Prime loader discovers all five project skills, expands /verdict-resume, and loads the context extension without diagnostics. Strict mypy and focused ruff validation cover the two helpers. MCP connection/discovery passed for all eight configured servers plus built-in Linear; GitHub CLI auth passed. No live issue/PR/merge was executed. Reviewer findings on termination, unknown daemon rosters and durable blockers were corrected and regression-tested.


## Hardening pass

- [x] Split durable state contracts into `scripts/prime_state.py` and keep `prime_workflow.py` as the validator/PR gate.
- [x] Bind receipts to packet dispatch and lease generation; reject malformed timestamps and stale proof.
- [x] Fence stale writers and ignore unregistered source/log churn in watchdog progress.
- [x] Use Prime's actual `session_before_compact` result contract for deterministic continuity summaries.
- [x] Add subprocess failure-injection and cold-resume lifecycle tests.
- [ ] Live PR/CI/merge/main verification remains an external-authority acceptance step.
