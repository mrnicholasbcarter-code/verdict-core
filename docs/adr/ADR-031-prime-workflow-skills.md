# ADR-031: Project-owned Prime workflow and bounded recovery

- **Status:** Accepted for implementation by user
- **Date:** 2026-09-13
- **Deciders:** Nick (repo owner)

## Context

Related: ADR-021 deterministic provider receipts; ADR-023 governed swarm supervision; ADR-030 proof-carrying decision plane. This workflow consumes their authorities and does not replace routing or public wire contracts.

The existing autonomous-development guidance does not define checkpoint, receipt, proof-opening or progress-timeout contracts. A stalled session repeated source reads for hours without new verification evidence.

## Decision

Five workflow skills own resumption, hydration, explicit dispatch, proof, and finishing. Prime 0.9.4 discovers project skills in `.prime/agent/skills`. Spec Kit remains lean specify -> plan -> tasks -> implement. Code-server is an optional human cockpit.

Linear owns objectives, priorities and dependencies. Git and GitHub own source, worktrees, PR checks and merge facts. Local orchestration checkpoints are hints reconciled with those sources on every restart. Memory and Sequential Thinking are advisory, never completion evidence.

All attempts bind issue, worktree, base SHA, exact target and attempt ID. Proof and review bind a clean head SHA; any source change invalidates them. State advances only with the evidence required by that state. A helper rejects incomplete proof before invoking `gh pr create`; this is a workflow gate, not a sandbox preventing arbitrary alternative network calls.

Use subprocess Prime clients when an explicit working directory and externally bounded runtime are required. Prime print clients still use daemon workers: assign unique session directories, stop/reap the owned client process group, stop owned daemon sessions and confirm their absence before retrying. RLM run is admission-only and has no cwd parameter; an idle/absent child is not proof. An external supervisor detects absence of substantive file/evidence progress even while messages or heartbeats continue. It persists recovery context, limits retries, and never kills unrelated processes based on PID guesses. A host-wide common-git lock serializes this loop across worktrees. Long commands need explicit bounded budgets. An unreaped or externally owned writer blocks redispatch.

Ownership is a durable per-issue lease, not a process lookup. `acquire_lease` records issue, `dispatch_id`, `worker_id`, worktree, branch, acquisition time, `last_progress_at`, `stale_after_seconds`, supervisor identity and a monotonically increasing `generation` that acts as the fence token; every acquisition is retained under `leases/<issue>/generation-<n>.json`. A live unstale lease blocks a second writer. A lease whose progress deadline expired is superseded automatically at the next supervisor start, so a stale multi-hour writer is recovered without an operator locating its process, and the fenced writer can no longer heartbeat, release, or pass `require_lease_for_pr`. Lease staleness bounds are configuration, not hard-coded task assumptions.

The worker receipt is both the terminal result and the progress record: `dispatch_id`, `issue_id`, `worker_id`, `started_at`, `last_progress_at`, `current_step`, `objective`, `files_changed`, `git_head`, `commands_run`, `tests_run`, `proof_collected`, `blockers`, `next_action`, `needs_rehydration` and `needs_escalation`, validated against the hydrated packet. Liveness is separated from progress: heartbeats update `heartbeat_at` only, and only a substantive result moves `last_progress_at`.

Progress detection hashes three components — substantive source/state, receipt identity, and registered artifacts — and accepts progress only on a substantive change or a receipt transition. Evidence-file churn alone is ignored, so an alive-but-hung worker cannot extend its own deadline by writing logs, and unregistered files never count.

`write_checkpoint` validates the full checkpoint field list, refuses to regress an issue to an earlier lifecycle state, and carries `completed_issues` across issue switches, which makes a cold restart deterministic: the next legal action is projected from disk, not from conversation.

CI results are classified deterministically (`GREEN`, `PENDING`, `CODE_FAILURE`, `INFRA_FAILURE`, `CANCELLED`, `MISSING_REQUIRED`, `EMPTY`) and merged with merge-state classification (`MERGEABLE`, `CONFLICT`, `BEHIND`, `UNKNOWN`) into one bounded recovery decision: code failure returns to corrective implementation in the same owned worktree, infrastructure failure retries without code changes, conflict rebase and reproves, and exhausted attempts block. A red or unclassifiable result is never reported as success.

The project context extension registers `session_before_compact` to inject deterministic continuity state at the real compaction boundary, and separately requests compaction at 120k tokens or 60% context, earlier than native thresholds for 500k models. Checkpoints remain authoritative over summaries. The supervisor permits two restarts by default (`--max-restarts`, validated 0..5) and then exits blocked with the reason persisted, so retries are bounded rather than looping. The existing documentation-and-adrs skill governs decision documentation.

## Alternatives considered

- Native autonomous completion gates alone: useful at finish, but cannot independently detect a stalled model/tool turn.
- New orchestration engine or memory store: unnecessary duplication of existing Prime/Verdict capabilities and authorities.
- RLM workers for writes: no explicit cwd parameter; retained daemon child lifecycle complicates exclusive ownership. Keep RLM for bounded read-only work and use owned session subprocesses for writes.

## Consequences

Reproducible discovery and local failure-path testing without model calls; external Linear/GitHub/model health is still a runtime prerequisite. A running legacy interactive session is not retroactively fenced by this supervisor: fencing applies to lease holders, so migrate only after stopping that writer and preserving its work. Lease and checkpoint state lives in the git common directory, which is shared by all worktrees of the repository and is not committed. Operator commands for inspecting supervisor state, workers, leases, the latest checkpoint and issue proof, for fencing stale writers, and for stopping and resuming safely are documented in `docs/guides/prime-workflow.md`.
