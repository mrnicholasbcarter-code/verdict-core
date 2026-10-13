# Operating standards

Operator decisions reconciled on 2026-10-12. This guide states engineering policy,
not proof that every described capability is implemented. [AGENTS.md](../../AGENTS.md)
owns enforced invariants. Follow the [release workflow](../../CONTRIBUTING.md) and
[autonomous development guide](autonomous-development.md).

## Deliver functionality and preserve ownership

- **Deliver functionality first.** Most effort and tokens must deliver working features or fix root causes, not repeatedly rerun process checks or design process before functionality exists.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Batch ready changes and run full gates once.** Integrate ready reviewed PRs into one branch, fix lint/conflicts/test failures together, then run full CI on that exact batch. Focused tests continue during development.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Workers own delivery to merge.** The worker acts as its own controller: decompose, prepare context, select agents, review, fix, integrate and own completion through merge. Retain one repository integration authority and serialized integration lock; coordinate rather than race main writes.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Keep multiple dependency-ready tasks queued.** Queue useful independent tasks; start them when host and provider capacity permits, preserving work so another quota pool can resume it.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Whole-repo release review.** Before release-train merge, inspect the whole repository as a working product, not only the diff. Revalidate production wiring, stale documentation, unsafe/dead/stubbed paths and cross-module contracts.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Track unfinished work once.** Value-rank unfinished, stubbed, unwired and inefficient code; update the existing story or create exactly one bounded story with AC, test criteria, proof and PR references. Record delete/defer decisions.
  Source: operator decisions and observed lessons, 2026-10-11.

## Resume from evidence

- **Current runtime truth beats memory and prose.** Evidence order: merged default-branch code; exact-head tests/CI/proof; current contracts/ADRs; GitHub history; Linear state; prose. Memory and checkpoints are hints reconciled against these facts, never completion evidence.
  Source: operator decisions and observed lessons, 2026-09-24.
- **Persist state before compaction or model switch.** At roughly 70–75% context pressure, checkpoint issue/worktree/branch/base and head SHAs, changes, decisions, proof, PR/CI, blockers and exact next action. Compact, reload/reconcile, continue; compaction is not completion.
  Source: operator decisions and observed lessons, 2026-09-21.
- **One story owns one worktree; one integration authority.** Parallel implementation uses disjoint file/authority ownership and dependency-ready contracts. Serialize integration and heavy gates; never alter the checkout under a running exact-head gate. Source changes invalidate prior review/proof. Attribute each result to the SHA actually tested, preserving failed runs separately from later successes.
  Source: operator decisions and observed lessons, 2026-10-10.

## Use qualified capacity, free first

- **Free-first means accepted work, not free calls.** Prefer a healthy, sufficiently qualified free route. Use included subscription capacity when no suitable free equivalent exists or the role needs it; optimize accepted/merged work, retries and review cost. Unknown quality is guarded exploration.
  Source: operator decisions and observed lessons, 2026-10-10.
- **Dispatch from measured quota, not fixed names.** Choose a qualified model and effort for each task/subtask from live host resources, provider/account quota, capability and role fit. Reserve controller capacity and warn before its pool exhausts.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Adaptive concurrency replaces fixed reviewer count.** Use the existing governor with measured available RAM, active swapping, memory pressure, load relative to cores, disk and provider/account limits. Reserve controller/shared-service headroom; increase gradually when safe and decrease on pressure, latency or throttling. Swap occupancy alone is not current pressure. The current deployment policy uses load below 6 and memory PSI `some avg60` below 10 alongside ownership, disk, provider and reserve checks. These are deployment constants, not universal product limits; validate changes before replacing safeguards.
  Source: operator decisions and observed lessons, 2026-10-07.
- **No new infrastructure spend without budget.** Already authorized provisioned capacity may be used automatically. Do not resize paid hosts or provision new infrastructure without a measured bottleneck, cost and approval.
  Source: operator decisions and observed lessons, 2026-10-07.

## Prepare context and fix causes

- **Prepare bounded worker context before dispatch.** Read AGENTS.md, ADRs/OpenSpec, source symbols/callers, tests, prior state, installed-tool help and current library docs. Brief every worker with exact owned files, AC, constraints, findings and verification.
  Source: operator decisions and observed lessons, 2026-10-11.
- **Research before larger decisions.** Compare plausible approaches/tools with evidence before choosing a major architecture or workflow. Prefer existing code, standards and installed tools.
  Source: operator decisions and observed lessons, 2026-10-11.
- **Classify test failures and fix the responsible mechanism.** Distinguish product defects from test design, environment leakage and obsolete assumptions. Repair the product or shared isolation/factory/interface contract as appropriate; do not weaken tests.
  Source: operator decisions and observed lessons, 2026-10-11.
- **Local CLI errors are not provider failures.** Offline smoke the actual generated worker argv against installed CLI. Removed -ne is local harness incompatibility and must BLOCK locally, not cool healthy routes or reselection-loop.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Bound context retrieval work, not just output.** Constrain allowed roots and traversal/read/parse work; use hermetic temporary repo fixtures rather than broad /tmp/shared artifact trees. Token/output caps alone do not prevent pathological scan or regex costs.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Ground planner in real production paths.** Give exact production caller/ownership and canonical OpenSpec paths; reject graphs naming nonexistent runtime files before execution.
  Source: operator decisions and observed lessons, 2026-10-12.
- **Worker admission is not success.** Require completed validated nonempty output and task verification. Normalize no reply, empty/malformed response, exception, timeout and 400/401/402/403/429/5xx into failed attempts; record cooldown and bound replacement on the same task.
  Source: operator decisions and observed lessons, 2026-09-23.
- **Eligibility shows the entire evaluated set.** Never silently truncate/top-N evaluated candidates. Every model and rejection stage/reason remains observable; JSON counts reconcile. Paging and explicit visible user filters are fine.
  Source: operator decisions and observed lessons, 2026-09-26.

## Publish observed claims and verify completion

- **Public demos and savings require real work.** Public demos, recordings, charts and savings claims must use real models doing real tasks and measured data. Fixtures are for clearly labeled offline tests only. Remove unsupported savings claims.
  Source: operator decisions and observed lessons, 2026-10-10.
- **Real-time polished demonstrations.** Do not speed up recorded demos or duplicate unexplained walkthroughs. Show meaningful TUI interactions and explain traces, receipts and charts in plain language with clear anchors and visual hierarchy.
  Source: operator decisions and observed lessons, 2026-10-10.
- **Challenge larger decisions cross-family.** Push back with evidence; use a different frontier model family for adversarial review or debate when available. A silent or failed reviewer is not approval.
  Source: operator decisions and observed lessons, 2026-10-11.
- **Review is evidence, not certification.** Reproduce review findings on current code, reject false positives, tie verified defects to existing stories, retest and independently re-review exact SHA. Clean OCR cannot replace live route, memory, E2E, CI or producer-attestation proof.
  Source: operator decisions and observed lessons, 2026-09-22.
- **Certification is exact-SHA and immutable.** Retain immutable digest-bound evidence for final main, all required steps including clean/chaos rehearsal and actual independent GitHub OIDC/Sigstore provenance. Hold release tag until CERTIFIED. INCOMPLETE/FAIL is not completion.
  Source: operator decisions and observed lessons, 2026-10-11.

## Decision records and evidence boundaries

- [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) records the
  capable-free-first operator amendment (2026-09-29 and 2026-10-10). Health probes
  are not role/session qualification.
- [ADR-033](../adr/ADR-033-shared-memory-provider.md) and
  [ADR-034](../adr/ADR-034-memory-outbox-mirror-and-fail-open-shared-recall.md)
  supersede only the conflicting memory authority in
  [ADR-030 §2](../adr/ADR-030-proof-carrying-decision-plane.md). Local
  [MemoryPlane](../adr/ADR-004-local-first-memory-plane.md)/
  [MemoryGate](../adr/ADR-009-durable-memory-write-gate.md) stay authoritative;
  shared providers remain optional/advisory. Other ADR-030 proof semantics remain.
- [ADR-031](../adr/ADR-031-prime-workflow-skills.md) binds attempts and proof to
  durable ownership and exact heads. [ADR-037](../adr/ADR-037-supervisor-concurrency-governor.md)
  owns readiness, collision and resource admission.
- [ADR-027](../adr/ADR-027-observed-free-status-and-context-omissions.md) keeps
  unknown free status and omitted requested context explicit.

Observed local failures motivate these standards; they do not prove that a fix
has reached main. Do not promote a design, closed story, mock or review summary to
runtime evidence. Keep bounded checkpoints and source references in durable project
state; do not put private session transcripts or credentials in public docs.
