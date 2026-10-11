# Controller operation safety design

## Context
See proposal.md. The controller publishes real progress only after events.
Buffered executor output is not progress. Prime deliberately starts a new session,
which means killing only the controller process group does not own its workers.

## Goals / Non-Goals
Correct the initial and repair planning waits and supervised Prime ownership.
Do not fabricate heartbeats or widen the stall budget. Do not claim safety for
selection refresh, DAG operations, or review subprocesses. Deliberate same-UID
cgroup escape or state tampering is outside this cooperative-controller contract.
Do not claim self-hosting COMPLETE proof.

## Decisions
- Separate atomic `controller-operations.json` state from `progress.json`.
  Each operation has phase, attempt, random ID, immutable monotonic start/deadline,
  controller PID/start ticks/boot/session identity and generation token. Exact
  scope exit marks that operation ended. Initial/repair calls have separate IDs.
- Supervisor validates first-read phase caps (600 seconds for planner/plan_repair),
  finite timestamps, owner, generation and fixed snapshots. Invalid or mutated
  records never defer ordinary stall detection. Expired valid operations trigger
  termination. Observed waiting is `WAITING_BOUNDED`, not HEALTHY or progress.
  A whole-call timeout also bounds direct HTTP planning I/O, not only each I/O phase.
- The total supervisor deadline is checked before accepting completion or exit,
  before every generation and before spawning a worker. Cancellation also revokes.
- Use a stable never-renamed lock file shared by controller, launcher and supervisor.
  A durable pending intent exists before worker spawn. A lightweight launcher in
  its new session registers PID/PGID/session/start ticks/boot under that lock.
  State is fsynced before exec and the lock is held through exec (CLOEXEC).
  Revocation therefore cannot cross the token-check/exec boundary. No Prime payload
  starts unregistered or after revocation. A launcher killed before registration
  leaves an unresolved pending intent, which blocks replacement. This does NOT
  claim absence of a briefly unregistered launcher.
- Require explicit `VERDICT_CONTROLLER_CGROUP_ROOT`, a writable delegated cgroup-v2
  subtree. Create only a unique token-named generation leaf; supervisor/controller
  stay outside. Validate mountinfo, mount/device binding, boot/inode, no symlinks
  and strict child path. Launcher reads the durable validated leaf and joins via
  cgroup.procs BEFORE taking the stable exec-gate lock, so even a stopped lock
  holder is contained. The lock then checks generation/intent and fsyncs before exec. Ordinary fork/clone/setsid descendants inherit it.
- Revoke before controller kill. Kernel cgroup.kill and bounded cgroup.events
  populated=0 are the worker-absence authority. There is no /proc scan fallback.
  Cleanup kills known containment BEFORE pending/revocation uncertainty is reported.
  Unknown lock acquisition fails immediately (LOCK_NB), not an event-loop hang.
  If revocation fails, cleanup attempts known containment from the atomic snapshot,
  but never claims verified revocation or permits replacement. PID identity mismatch
  never authorizes signalling unrelated processes; no unverified killpg is used.
  After killing lock-owning launchers, an uncertain revoke is retried to fence
  still-unjoined pending launchers, but the uncertainty still blocks replacement.

- A restarted supervisor refuses an unknown/live prior controller rather than
  adopting or killing an unverified process. Pending controller spawn state also
  blocks recovery. Supervised launch failures remain bounded and explicit.

Alternatives rejected: blanket stall increase, fabricated progress, killpg on
unverified persisted PIDs, heuristic /proc scans for fork closure, and a complex
subreaper fallback. Explicit delegated cgroup containment is the supported path.

## Risks / Trade-offs
- Linux cgroup-v2 delegation required → missing explicit root or unsupported kernel blocks
  supervised Prime BEFORE payload/provider execution; unsupervised behavior unchanged.
- Custom planner timeouts over 600 → do not receive a lease exemption. This
  increment supports the current production 600-second default. Logical owner/phase/attempt identities pin their first operation
  ID and deadline. Only coordinator-assigned plan-1 through plan-6 (and repair) receive
  exemptions. A custom wider attempt budget needs an explicit reviewed contract change.
- Deliberate same-UID cgroup migration or state tampering → outside the trust model;
  normal fork/setsid is contained. No elevated permissions or resource controllers required.
- Hard death at pending registration → BLOCKED until operator investigation;
  availability is sacrificed rather than guessing worker absence.
- Operation-state files are controller-owned coordination, not a security boundary
  against a malicious controller with write access to its run directory.

## Migration Plan
Deploy reviewed code with the planner bridge; do not resume a live run before
focused tests and independent review pass. Older runs without ownership records
cannot prove previously detached workers absent. Operator cleanup is required.
Rollback removes the new bridge but restores old watchdog/orphan limitations;
never delete pending ownership state as an automatic rollback step.
