# ETHOS — read this first

What Verdict is for, what is in force, and what is being worked on now.
Agents and humans read this before AGENTS.md. Only the operator changes
Mission, Decisions and Parked; agents update NOW after each step.

## Mission

[BOD-342 D15](https://linear.app/bodanglin/issue/BOD-342): free tokens are a force
multiplier. Every task a free model does safely is extra work at no cost.
Verdict routes each prompt to the cheapest model proven capable of that task,
using live OmniRoute evidence. The headline metric is accepted work per paid
dollar, and per % of paid quota. Memory, context packs and outcome feedback all
exist to serve this. New work must say how it advances it.

## In force

- Cheapest qualified tier first, then strongest.
- Tool proof before dispatch.
- Planners: strongest admissible model in the frontier band; paid last.

## NOW

Next focus (operator, 2026-11-23): Verdict builds a GitHub issue through to a PR ready for the operator's merge ([ADR-052](docs/adr/ADR-052-github-issue-to-ready-pr.md), D37). `verdict deliver` is merged and installed (#1355, #1371). On 2026-11-26 Verdict built a story with `verdict orchestrate` on an OpenCode worker and delivered it as a ready pull request by itself (#1373, issue #1269 task 6.1); the DoD evidence for #1269 is not filed yet. Self-build runs still need a dictated node plan and `--scope cc/,cx/`. On 2026-11-27 a story went from `verdict orchestrate` to a ready pull request with no controller code change (#1379). Done since: #1369 (closed), and for #1372 the commit of git-ignored owned files (#1377, #1379) and the retry that continues from a failed verification for OpenCode workers. Next: #1372's post-merge checks and its unowned ignored files, #1366 part 2 (recover by denial reason), and #1367.

## Decisions

Decisions: Linear BOD-342. Failures: BOD-348. Read both before changing prior
work. Memory = Hindsight (D33 / ADR-050; supersedes D1). ai-memory fully
decommissioned (D34 / MEM2 #1254).

## Parked

- Full gateway redesign (V3).
