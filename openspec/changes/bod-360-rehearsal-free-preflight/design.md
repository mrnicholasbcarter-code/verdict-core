# Design

## Context

`run_certification_rehearsals.py` runs `verdict orchestrate --capacity free`
with a fresh `VERDICT_HOME`. The planner needs tools, so the ladder requires a
fresh agentic PASS for each FREE route. A fresh home has no health cache.

## Goals / Non-Goals

Goals: a cold rehearsal home gets genuine fresh agentic proof for a few FREE
routes, and the rehearsal stops before orchestrate when none passes.

Non-goals: changing admission, the agentic gate, capacity policy or the
planner floor; copying proof from another home; screening non-FREE routes.

## Decisions

- Use `verdict identify --screen --capacity free`, not `prove-at-rest once`.
  `prove-at-rest once` has no capacity filter: its chat phase also probes
  subscription routes. The screen filters by capacity before any call, runs
  the same three-turn agentic conversation and writes the same health cache.
- Bounds: `--max-per-provider 2`, `--max-routes 4`. The screen has its own
  600 s internal deadline; the subprocess timeout is 630 s.
- Decide on the screen's own counters, `routes_checked` and `passed`. The
  screen counts a pass only when the agentic probe passed.
- Error messages are fixed strings. The child's stdout and stderr are never
  copied into them.

## Risks / Trade-offs

- Four routes may all fail on a bad day. The rehearsal then fails closed with
  a named error rather than running a doomed orchestration.
- A passing screened route might still miss the planner's context or tier
  floor. Orchestrate then fails as before, with its own diagnostics.

## Security / trust implications

FREE calls only. No proof is copied or synthesized. The key and gateway URL
never appear in preflight errors.

## Routing / context / memory implications

Routing reads the health cache the preflight wrote; ranking and admission are
unchanged. No context-pack or memory change.

## ADR impact

None. This applies the existing agentic gate (tool proof before dispatch);
it does not change it.

## Migration / rollback

Revert the script change. No stored state changes shape.
