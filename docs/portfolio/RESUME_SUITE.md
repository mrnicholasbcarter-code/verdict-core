# Senior / Staff Engineer Resume Suite

These are role-targeted drafts for the [Verdict Core repository](https://github.com/mrnicholasbcarter-code/verdict-core). Use branch-qualified language until the orchestration branch is merged. The [certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) records a fresh-clone test and live runs; it does not certify production use or a merge to `main`. Run artifacts are under `~/.verdict/evidence/golden-path/` (not part of the public repository).

## Master resume — AI infrastructure and reliability

### Summary

Engineer building policy-gated AI execution systems in Python. Designed Verdict's goal-to-receipt orchestration: dynamic eligibility and capacity-aware route selection, bounded same-node recovery, integration checks, independent AI code review and integrity-linked receipts. The work is demonstrated by the [golden-path certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) and [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md), not by production adoption metrics.

### Project: Verdict Core — Creator / Lead Architect

- Implemented a frontier-planned DAG with parallel, isolated worker attempts. The planner does not choose worker routes; Verdict applies eligibility and ranks only routes that pass it. See [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
- Classified quota, rate limit, auth, transport, timeout and 5xx failures. Recorded route- or provider-scoped cooldowns and reassigned the **same node** to another eligible model. When the pool was exhausted, the run stopped as `BLOCKED` with an integrity-linked receipt, rather than silently succeeding. Evidence: [certification scenarios B–H](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md), runs `live7`, `live9`, `live11`, `live12`.
- Added an independent OpenCodeReview gate after integration. Reviewer selection excludes implementer routes; a blocking or failed review cannot produce `COMPLETE`. A resumed run also restores previous implementer routes. Evidence: [certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md), `live10` receipt and `tests/test_orch_resume.py`.
- Certified the branch from a clean clone: `ruff check .`, `mypy --strict verdict/`, and **2907 passed** in the full pytest suite, then a live `certlive` run reaching `COMPLETE` after integration and review. This is branch-local certification, not merged-main CI or a production reliability claim. [Certification record](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).

### Skills demonstrated

Python / AsyncIO; policy and eligibility gates; process supervision; failure classification; git worktree isolation; test and integration barriers; event logs and receipt integrity; model-provider capacity management.

## Variant A — AI infrastructure

**Focus:** Decision authority and review integrity.

- Built a dynamic eligibility ladder for planning and worker assignment: live discovery, entitlement and harness visibility, health, cooldown availability, task eligibility, then selection. Subscription/free/metered capacity evidence affects ranking only after hard gates. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
- Bound `COMPLETE` to validated nodes, a passing integration barrier and a passing independent review. A missing review, failed check or exhausted eligible pool yields a `BLOCKED` receipt. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md); [run `live7` and `certlive` evidence](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).

## Variant B — Distributed systems and reliability

**Focus:** Fault isolation and recovery.

- Reassigned the original work node after model-scoped or provider-scoped quota failures, with explicit cooldown scope and reset time in the receipt. Fault-injected `live9` and `live12` exercise different scope and provider switches. [Scenario matrix](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
- Built bounded controller supervision: the `live10` supervisor detected a stalled generation, terminated its process group, resumed previously validated work, and completed with a different-family reviewer. [Certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md); `~/.verdict/evidence/golden-path/live10-run/receipt.json`.

## Variant C — Verification and developer experience

**Focus:** Inspectable outcomes instead of unqualified success claims.

- Produced a secret-scrubbed append-only event log and receipt digest. The `run-receipt` command recomputes integrity; the branch's completed live runs passed receipt checks. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md); [certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
- Maintained a credential-free `verdict quickstart --non-interactive --dry-run` alongside the live goal-to-receipt path. The quickstart is a fixture, **not** a live-provider benchmark; both commands were checked in the [fresh clone](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).

## Claims boundary

The certification explicitly does **not** cover merge to `main`, merged-commit CI, an automatic remediation/re-review loop after blocking review, or adaptive concurrency. Do not describe the branch as a production deployment or extrapolate a success rate from its finite live runs.
