# Design — research-stage bench (RS1)

## Reuse from FU1

RS1 depends on three parts of the FU1 harness (`verdict/fusion_plan_bench.py`). FU1's addendum asks for them to be factored out. RS1 imports them and does not copy them:

| Part | Used for |
|---|---|
| Task set format | A task is a repository commit before the fix, a node objective, the owned files and the verification command. |
| Arm runner | Runs one worker attempt with a given pack on a clean worktree at the task's commit and returns the terminal result and token counts. |
| Baseline comparison | Pairs arms on the same task and reports differences with per-task detail. |

If FU1's factoring is not merged, RS1 waits. It does not fork the harness.

## Arms

| Arm | Pack |
|---|---|
| A (control) | `build_worker_context` as today. |
| B (treatment) | Up to 3 free researchers gather findings for the node objective. A frontier model reads the cited primary sources, drops findings whose citation fails, and writes the pack within the same token budget the worker gets. |
| C (size control) | Arm A's retrieval extended with its next-ranked results until it reaches arm B's pack size in tokens (N25). |

The worker route, worker budget, tools and verification command are the same in every arm. Only the pack differs.

### Arm C padding

- **Source:** `build_worker_context`'s own ranked retrieval for the task. Arm C takes the items arm A cut, in the
  same order. Ties keep that function's existing deterministic order (rank, then path), so the same task always
  pads the same way.
- **Stop:** add whole items until the next one would take the pack past arm B's token count, counted with the
  same tokenizer the worker budget uses. An item is never truncated.
- **Check:** padding never goes over arm B's size, because it stops before an item would exceed it. The padded
  pack is valid when its size is at least 95% of arm B's (95% ≤ C ≤ 100% of B). Below 95% it is valid only if
  retrieval ran out of items. Any other result is reported as `size_control_invalid`, and the task is excluded
  from the A/B/C comparison.
- **Report:** the token count of all three packs, per task.

## Researchers

- Researchers use `FREE` routes admitted with tool proof, read-only. The bench grants only read tools: read file, search code, read doc, memory search.
- Each researcher has a hard cap on calls and tokens, set in the bench config and recorded in the report (N47).
- A finding is `{claim, source}`. The source is a file and symbol, a doc path, an ADR, a memory page id or a URL. A finding without a source is dropped before synthesis.
- Single-node plans skip research. The task set therefore draws nodes from multi-node plans only.

## Synthesis

- The synthesis runs on the strongest admitted planner-role route.
- It opens each cited source and keeps a finding only if the source supports it.
- Each dropped finding is a citation error (N43). The count is reported per task.

## Metrics

| Metric | Definition |
|---|---|
| First-attempt pass rate | Share of tasks whose verification command passes on the worker's first attempt. |
| Frontier tokens per accepted node | All frontier tokens in the arm (research synthesis included) divided by the accepted nodes. |
| Free tokens | All free-route tokens in the arm (researchers included). |
| Citation errors caught | Findings the synthesis dropped. |
| Repeated-research share | Share of arm B's kept findings already present in arm A's pack. |

## Task set

The first run uses about 6 tasks, drawn from stories closed on main. Each task:

- has a node whose objective needs repository or decision lookup;
- has a deterministic verification command;
- has a pre-fix commit that still builds.

The criteria and the chosen list are committed with the task set before the first run.

## Stop rule

The stop rule follows the shape of D18. It is written before the run as a file the bench requires (`--stop-rule`).

**File format.** A YAML file with these required keys:

- `options`: a list;
- `expected_result`: a string;
- `checkpoints`: a list. Each entry has `after_tasks` (an integer), `continue_if` (a condition over the report's per-arm metrics, for example `B.first_attempt_pass > max(A.first_attempt_pass, C.first_attempt_pass)`) and `else` (a string: the action when the condition fails);
- `decided_by`: a string.

The bench rejects a file that is missing a key or has a condition it cannot parse, and makes no model call. The rule's hash is the SHA-256 of the file's bytes, and the report embeds it.

- **Options:** adopt the research stage, change it, or drop it.
- **Expected result:** arm B passes more tasks on the first attempt than both A and C, without more frontier tokens per accepted node than A.
- **After the first ~6 tasks:**
  - If B does not pass more tasks than both A and C, stop. Record the result in `docs/decisions/NEGATIVE_RESULTS.md` and drop or change the design.
  - If B is ahead, run to about 20 tasks.
- **After ~20 tasks:** the operator decides adopt, change or drop. Nothing enters the runtime before that decision.

## Safety and budget

- The bench makes live calls only with explicit consent and a budget, like the FU1 bench: a call cap, a token cap and a per-call timeout.
- Researchers never write.
- The frontier synthesis uses subscription capacity, never pay-per-token, unless the operator's cost policy allows it.
