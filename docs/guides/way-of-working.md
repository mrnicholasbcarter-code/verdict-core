# The way of working

How stories are built in this repository, as the operator approved on
2026-11-28. Follow it when you build a story, whether you are an agent or a
person. [AGENTS.md](../../AGENTS.md) owns the enforced invariants, the Definition
of done and the push gate. This guide does not replace them.

Why it exists: accepted work per paid dollar is the mission
([BOD-342 D15](https://linear.app/bodanglin/issue/BOD-342)). An audit of 69
pull requests of the week to 2026-11-28 found these:

- 16 pull requests over 1,000 lines held 70% of all fix commits. They took a
  median 7.8 h to merge, against 0.8 h for the rest.
- Only 6 of 21 workflow builds ended ready. 12 stopped at the review step.
- Design pull requests waited a median 2.8 h and up to 34.6 h.
- A story took a median 58.8 h from issue to close.

## 1. Roles

The controller is the most expensive model. It does four things: it slices work,
writes briefs, starts agents, and checks their reports and decides. It does not
do the building.

Builders and researchers run on Codex models; the independent review runs on a different model family.

Everything else goes to a cheaper agent:

- Building and fixing: the builder agents, `verdict-builder` and
  `verdict-builder-small`.
- Research: the researcher agent, `verdict-researcher`.
- Code lookups (who calls this, which tests cover it, where it is defined): the
  code-graph agents, `codebase-memory` and `codebase-memory-scout`.
- Mechanical work: a mechanical-work agent on a small Codex model (added by
  [#1399](https://github.com/mrnicholasbcarter-code/verdict-core/issues/1399)).

## 2. Two lanes

**Fast lane.** This is the default. A change is fast lane when a revert undoes it
and it does not touch who is eligible, spend, credentials or the invariants.

A fast-lane story is code, tests and a short behaviour note in one pull request.
It gets one review round. It is merged on green and summarised afterwards. If
the change alters behaviour, its OpenSpec delta goes in the same pull request.

**Slow lane.** A change to eligibility, spend, account risk or an invariant gets
a design first, and the operator approves the design before the build starts.

## 3. Slices

One story is one pull request of about 400 changed lines and 5 files. When
stories are grouped into an `integration/<epic>` branch, steps 4 to 6 of the
release train in [AGENTS.md](../../AGENTS.md) still apply to that branch.

Cut the smallest slice that moves a number. Measure it for a day. Then decide the
next slice.

## 4. The path of a story

1. **Name the lane.** Fast or slow.
2. **Write the brief from facts.** Prior decisions come from memory (Hindsight).
   The callers and the tests the change touches come from the code graph.
3. **One builder per slice.** The builder writes the failing test first.
4. **Cap the local check loop.** The builder runs it for at most three rounds. It
   stops earlier if the same failure repeats.
5. **One independent review.** A model from a different family reviews against the
   issue's acceptance checks. Each finding is marked blocking or follow-up.
6. **CI and merge.** Merge on green.
7. **Measure.** The next day, record the result on the issue.

## 5. Parallel work

Up to three builders may run at once. They run only on stories whose files do not
overlap. Compare the file lists before you start a second or third builder.

Never put two writers on one slice.

## 6. Workflows

A workflow is for breadth jobs: research sweeps, audits and multi-angle reviews.
Keep each one small.

Do not use a workflow to write one story's code.

A workflow takes no input mid-run. If a job needs a sign-off point, run it as
separate workflows, one on each side of the sign-off.

## 7. Tools

| Tool | Use |
|---|---|
| Code graph (codebase-memory) | Its impact list goes into every brief. It adds tests to a brief and never replaces CI. |
| Library docs (Context7) | Only when a story touches an outside library. |
| Memory (Hindsight) | Searched at intake. |
| Structured notes (working-notes) | Only for a failure that repeats, together with the debugging skill. |
| Symbol tools (Serena) | Optional, for rename-heavy stories. |

## 8. Research

Spend about 30 minutes on a fast-lane question, then decide on what is known.

Do deep research only when the operator asks for it or the decision is slow lane.

## 9. Week-one measures

These are the measures for the first week, with their baselines where the audit
gave one:

- First review clean on at least half of pull requests. The baseline is 4 of 10.
- Mean review rounds at most 1.7.
- Median time from first commit to merge at most 1.6 h.
- More pull requests at or under 400 lines. The baseline median is 552.

Tokens per merged pull request has no baseline yet.
