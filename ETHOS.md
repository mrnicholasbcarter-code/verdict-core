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

## NOW

Next focus (operator, 2026-11-23): Verdict builds a GitHub issue through to a PR ready for the operator's merge ([ADR-052](docs/adr/ADR-052-github-issue-to-ready-pr.md), D37). Delivery comes first (`openspec/changes/verdict-deliver`), and Verdict builds each pipeline story itself through `verdict orchestrate`. In flight: ADR-051 frontier planner (#1266) and CP2 connection billing facts (#1268). Merged and installed on 2026-11-23: #1259 (community roster), #1260 (context-lift supersede), #1261 and #1267 (relay empty reply, serve budget), #1262 (contested proof rescreen), #1264 (pinned metered executor). Their DoD evidence comes after the freshness window.

## Decisions

Decisions: Linear BOD-342. Failures: BOD-348. Read both before changing prior
work. Memory = Hindsight (D33 / ADR-050; supersedes D1). ai-memory fully
decommissioned (D34 / MEM2 #1254).

## Parked

- Full gateway redesign (V3).
