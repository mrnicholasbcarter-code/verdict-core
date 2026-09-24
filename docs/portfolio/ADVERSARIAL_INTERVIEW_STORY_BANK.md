# Adversarial interview story bank — goal-to-receipt work

These are STAR prompts for technical interviews, not independent attestations of individual authorship. The [fresh-clone certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) and run files under `~/.verdict/evidence/golden-path/` establish the technical observations. Run files and the incident note are local operator evidence, not checked into the public repository. ADR-036 describes the [shipped branch behavior and limits](../adr/ADR-036-goal-to-receipt-orchestration.md). Do not call this a production deployment or a merged-main CI result.

## Story 1 — Separating gateway admission from model failures

- **Situation:** Parallel coding-agent requests through OmniRoute v3.8.50 saw `503 chat_admission_busy`. A separate long agent/reviewer turn could return a local `504`. Both could be misread as model failures.
- **Task:** Find the bottleneck before penalizing a healthy route or interpreting an accepted SSE stream as completion.
- **Action:** The [golden-path runbook](../guides/interview-golden-path.md) identifies upstream OmniRoute issue `diegosouzapw/OmniRoute#13648` and records the host's admission/headroom/queue settings for parallel agents. Admission to a stream is not a final answer: `PrimeHeadlessExecutor` reports `no_final_answer` when no usable final response arrives (`verdict/orchestration/executors.py`). Separately, the Claude OAuth connection's limiter needed `rateLimitOverrides.maxWaitMs=120000`, because the global 15 s limiter expiry produced local `504` on long turns. Verdict classified `gateway_busy` as an infrastructure retry without cooling the model route; it does not claim to fix the upstream server. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md); `verdict/orchestration/recovery.py`.
- **Result:** The certified host used those prerequisites; `certlive` completed after parallel work, integration and review. The [scenario matrix](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md) reports live recovery under timeout and 5xx faults. It does **not** prove the upstream issue is fixed or that `503`/`504` can never recur.
- **Adversarial question:** *Isn't a 503 just another failed provider call?* No. A gateway-local admission shed has different scope from model quota. Penalizing the model for the gateway queue hides usable capacity. Admission to an SSE stream also does not prove a final answer.

## Story 2 — Reviewer independence across controller restart

- **Situation:** Independent AI code review must not use an implementer route. A controller resume initially restored validated commits but dropped their implementer route identities. The live resume exercise exposed this gap.
- **Task:** Preserve reviewer exclusions when prior work is reused instead of rerun.
- **Action:** Restored `(commit, route_id)` from validated node events to resumed runtime state; kept the review exclusion based on **all** implementer routes, including routes used before restart. Added `tests/test_orch_resume.py::test_resume_restores_implementer_routes_for_review_independence`, which injects a prior validated event and asserts its route reaches the reviewer exclusion set. Also prevented the live view from treating a prior controller life's final event as the current result. See repository commit `08a09a5` and [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
- **Result:** Recorded run `live10` resumed a previously validated node after a stalled controller generation; `live10-run/receipt.json` records `REVIEW_INDEPENDENCE` exclusions of the previous Claude implementer routes, a `cx/gpt-5.5` reviewer, review `PASS` and outcome `COMPLETE`. [Certification scenario I](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md). This demonstrates a route-exclusion invariant on that run, not a claim that an AI review detects every defect.
- **Adversarial question:** *Why not trust a different model name after restart?* Because the previous controller's implementer identity must survive the restart; otherwise the reviewer could be the model that made the change. The regression test covers identity propagation; the receipt records the actual reviewer selection.

## Story 3 — Replacing a quota-exhausted frontier planner

- **Situation:** Frontier planning itself uses a model. If its provider's quota is exhausted, no DAG exists yet and a naive worker-only retry policy cannot recover.
- **Task:** Apply the same bounded selection and cooldown discipline to the controller's planner call, without treating a failed plan as a valid graph.
- **Action:** Classify quota, scope the cooldown to the affected provider, and reselect another eligible frontier model before attempting graph validation. [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md) specifies that planner recovery uses the same classify/cooldown/reselect loop. The fault is explicitly tagged as injected in the recorded attempts.
- **Result:** `live7-run/receipt.json` records quota on `cc/claude-fable-5`, provider `cc` cooldown and replacement by `cx/gpt-5.5`, which produced a plan. `live11` demonstrates the reverse (`cx/gpt-5.5` to `cc/claude-fable-5`). Both runs later exhausted worker capacity and ended `BLOCKED`; the planner recovery did **not** make the entire runs `COMPLETE`. [Certification scenario I and H](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md).
- **Adversarial question:** *Is this an arbitrary fallback chain?* No. The second planner was selected from routes that passed eligibility after the cooldown. The exhausted worker pool still failed closed rather than inventing an outcome.

## Story 4 — A read-only audit worker used a destructive scratch command

- **Situation:** A worker prompted to remain read-only (`audit-c-cli`, route `cx/gpt-5.6-terra`) ran `rm -rf /tmp/vgp && mkdir -p ...` in its own shell tool call at 10:45Z on 2026-09-24 to create a scratch HOME.
- **Task:** Contain the damage, learn how it happened, and continue the audit without granting the worker a false safety guarantee.
- **Action:** The controller noticed `/tmp/vgp/sandbox-repo` was missing on its next command, then inspected the worker session journal and found the tool call. It immediately deleted/cancelled that worker, sent explicit rules to the two sibling audit workers, and re-dispatched to a different route (`cx/gpt-6-sol`) with an ABSOLUTE RULES block banning destructive commands and requiring private `mktemp -d` scratch. [Controller incident note](#evidence-boundary) in `~/.verdict/evidence/golden-path/incident-worker-rm-rf.md`.
- **Result:** The loss was confined to the controller's `/tmp/vgp` scratch (sandbox repo, audit report and logs). Repository code, branches, worktrees and `~/.verdict/evidence` were unaffected. The replacement finished read-only and reported concurrent controller commits instead of acting on them. This is a controller-observed incident, **not** a product test or proof of prevention; the original worker journal was removed with the worker.
- **Adversarial question:** *Did the read-only prompt intercept the shell command?* No. Detection was **after the fact**, when the directory was missing and the controller inspected the journal. There is **no automated shell interception** in this incident. A prompt is policy, not an OS-level enforcement boundary.

## Evidence boundary

- [Interview golden-path certification](../proof/INTERVIEW_GOLDEN_PATH_CERTIFICATION.md), especially scenarios G–J and the explicit non-certified items.
- [Interview golden-path runbook](../guides/interview-golden-path.md) and [ADR-036](../adr/ADR-036-goal-to-receipt-orchestration.md).
- Local receipts: `~/.verdict/evidence/golden-path/{certlive,live7,live10,live11,live12}-run/receipt.json`. For the containment story, `~/.verdict/evidence/golden-path/incident-worker-rm-rf.md` is the controller's observed incident note. The removed worker journal cannot be independently re-inspected from these files.
- The certification recorded **2907 passed** on a fresh clone; it did not certify a merge to `main`, automatic review remediation or adaptive concurrency.
