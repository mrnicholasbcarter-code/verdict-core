# Ecosystem Product Overview and Vision

Status: working product strategy, last reviewed 2026-07-18.

This document defines the product family that `llm-gate` should lead, the
boundaries between the surrounding tools, and the evidence required before the
portfolio makes public claims. `llm-gate` is a working name until a separate
name, package, trademark, and migration study is complete.

## Executive brief

The flagship product is a policy-safe, availability-aware execution control
plane for heterogeneous LLM and agent stacks. It turns a request into a
versioned task specification, excludes candidates that cannot legally or
reliably run it, selects a model or bounded workflow, verifies the result, and
records privacy-safe evidence for later improvement.

The product is not another provider proxy and it is not an unconstrained
autonomous-agent framework. OmniRoute already provides the unified model
transport. Hermes already provides durable user interaction and orchestration.
Ruflo can provide an optional workflow meta-harness, and RuVector can provide
an optional retrieval and graph substrate. The flagship earns its place by
being the deterministic authority between intent and execution:

```text
request
  → structured plan
  → policy, capability, availability, privacy, and budget gates
  → model or bounded workflow assignment
  → execution and verification
  → explainable outcome evidence
  → advisory learning that can never bypass a hard gate
```

The public ambition is to reach the engineering and product quality associated
with widely adopted 20,000–40,000-star open-source repositories. That is a
quality and adoption goal, never a claim that popularity can be engineered or
that unfinished software is production-ready.

## One portfolio, two coherent product lines

### Agent and LLM control plane

`llm-gate` and `llm-gate-node` form the primary product line. They make model
and workflow execution predictable across providers, clients, and agent
frameworks.

The north-star experience is:

1. Install one package or use one container.
2. Run a wizard or provide a declarative configuration.
3. Connect an existing OpenAI-compatible client.
4. See which candidates were considered, which were excluded, why one was
   selected, what it cost, and whether the result passed verification.
5. Add Hermes, OmniRoute, Ruflo, RuVector, or Hindsight through documented,
   optional adapters rather than private database coupling.

### Deterministic decision systems

The trading repositories are a separate vertical united by the same design
philosophy: explicit contracts, fail-closed gates, deterministic calculations,
and evidence-backed claims.

They are not runtime dependencies of `llm-gate`. They demonstrate how the same
engineering discipline applies to financial decisions:

```text
market evidence
  → feature and expected-value gates
  → backtest and falsification evidence
  → capital-protection authority
  → human-observable trading cockpit
```

Keeping the lines separate prevents a broad portfolio story from becoming a
coupled monolith.

## System architecture and authority

```text
phone / laptop / tablet
          │ SSH or private VPN
          ▼
Hermes: durable interaction, sessions, schedules, bounded delegation
          │
          ▼
llm-gate: contracts, hard gates, planning, selection, explanation ─────┐
    ▲                ▲                                                 │
    │ bounded plans  │ advisory evidence                               │
    ▼                │                                                 ▼
Ruflo: optional    RuVector: optional retrieval/graph        OmniRoute: unified
meta-harness       substrate                                provider/model plane
                                                                    │
                                                                    ▼
                                                             eligible providers

Hindsight: secret-free operator/session continuation memory
GitHub: issues, review, exact-SHA CI, release, and provenance control plane
```

| Component | Product responsibility | Authority boundary |
|---|---|---|
| Hermes | Channels, sessions, scheduling, durable orchestration, delegated tool use | Does not decide whether a model or workflow is eligible |
| `llm-gate` | Versioned contracts, hard gates, planning, deterministic routing, explanation, verification policy | Sole authority for route eligibility; learned signals are advisory |
| OmniRoute | Credentials, provider transport, OpenAI-compatible model plane, catalog, and any explicitly documented runtime evidence | Does not become permanent product policy or imply that catalog presence proves readiness |
| Ruflo | Optional workflow compilation, bounded swarm coordination, replanning and verification hooks | Cannot expand task scope or bypass a gate |
| RuVector | Optional semantic, graph, trajectory, and outcome retrieval | Cannot directly change a production route |
| Hindsight | Cross-session operator recall and sanitized resume checkpoints | Is not live health, quota, price, or policy authority |
| GitHub | Planned work, human review, source provenance, exact-SHA automation and releases | Status is evidence-based; a label is not proof |

There must be one routing authority. Integrations exchange versioned contracts
and evidence; they do not read one another's private databases or duplicate
policy.

## Flagship capability roadmap

This section separates the current alpha foundation from release targets:

| Status | Capability |
|---|---|
| Implemented alpha | Python `TaskSpec`, `WorkflowPlan`, `RoutingDecisionContract`, and `OutcomeEvent` contracts; deterministic fixtures and demo |
| Implemented alpha | Availability normalization, explicit unknown state, and fail-closed contradictory-signal precedence |
| Implemented alpha | Local adapter-protocol aliases for catalog and injected runtime/capability evidence; no complete live OmniRoute health/quota contract yet |
| In progress | Estimated-token/cost normalization, capacity/planner completion, end-to-end enforcement, intelligence, legal retry/fallback, and explanation parity |
| Planned release gate | Bounded Ruflo workflows/swarms, RuVector advisory retrieval, installer/wizard, cross-language parity, benchmarks, security, packaging, and release evidence |

The target state below is not a shipped-feature list.

### Target deterministic foundation

- Strict, versioned contracts shared across Python and TypeScript.
- Hard policy, privacy, capability, context, budget, and availability gates.
- Fail-closed treatment of stale, contradictory, incomplete, unhealthy,
  quota-exhausted, locked-out, or incompatible runtime evidence.
- Legal, bounded retry and fallback semantics that preserve request safety.
- A complete explanation for every exclusion, selection, fallback, and stop.

### Target useful intelligence

- Structured planning that decomposes only when decomposition is beneficial.
- Selection based on measured capability, quality, latency, cost, and
  availability rather than a permanent model-name tier.
- Bounded parallel workers with isolated worktrees, explicit termination,
  backpressure, provenance, review, and integration gates.
- Observe-only learning first; promotion requires replay, benchmark,
  rollback, drift, and safety evidence.
- Privacy-safe task/outcome episodes and retrieval with source attribution.

### Target product experience

- A five-minute credential-free quickstart and an optional live OmniRoute demo.
- One-command install, idempotent uninstall, interactive wizard, and
  non-interactive CI configuration.
- CLI, API, proxy, Python, and TypeScript surfaces with the same semantics.
- A local explanation/trace view that redacts secrets and upstream internals.
- Honest compatibility matrices for clients, providers, operating systems, and
  optional integrations.
- Reproducible benchmark fixtures and raw outputs for quality, cost, latency,
  availability, and fallback behavior.

### Target operations and trust

- Loopback/private-network defaults, explicit public-bind warnings, least
  privilege, and no secret-bearing probes or logs.
- Versioned configuration, migrations, snapshots, rollback, and bounded
  retention.
- Built-artifact install tests, dependency and secret scans, static analysis,
  threat modeling, and release provenance.
- Exact pushed-SHA CI watching and issue evidence for every atomic change.

## Users and jobs to be done

| User | Job | Product promise |
|---|---|---|
| Solo builder with many paid/free providers | Use the best currently legal model without manually changing clients | One endpoint plus an explainable, fail-closed control layer |
| Agent operator | Run parallel work without duplicate writes, runaway fan-out, or lost context | Bounded plans, isolated workspaces, verification, and durable resume |
| Platform team | Enforce privacy, budget, capability, and provider policy across tools | Versioned policy and evidence independent of any one framework |
| Library author | Embed routing without adopting the full stack | Small Python/TypeScript contracts and optional adapters |
| Evaluator or researcher | Compare routes and workflows reproducibly | Exportable fixtures, outcomes, limitations, and raw benchmark evidence |

## Repository family

| Repository | Verified role | Relationship and next product gate |
|---|---|---|
| `llm-gate` | Python flagship and current contract/policy authority | Complete P0 availability, planner, intelligence, retry, swarm, product, and release gates |
| `llm-gate-node` | Alpha TypeScript/Express implementation for OpenAI-compatible clients | Publish an explicit parity matrix; do not imply full Python parity until verified |
| `backtest-harness` | Monte Carlo and walk-forward evaluation for prediction-market strategies | Repair baseline and reproduce fee, scale, and performance claims |
| `edge-mining-framework` | Pure feature evaluator plus fee-aware expected-value and Kelly gate | Verify package, mathematical, latency, and anti-lookahead claims |
| `trade-risk-engine` | Deterministic capital-protection and desk-control evaluator | Reproduce latency, allocation, coverage, fuzzing, telemetry, and state claims |
| `trading-cockpit-ui` | Next.js/TypeScript human operations surface for trading and risk telemetry | Add test/build evidence, realistic WebSocket behavior, accessibility, screenshots, and deployment proof |
| `mrnicholasbcarter-code` | GitHub profile and portfolio narrative | Publish only claims already proven by current repository evidence |

The local `hermes-plugins` checkout points to the third-party
`42-evey/hermes-plugins` repository. It is an upstream evaluation source, not a
Nick-owned portfolio product and not a push target. Original Hermes integration
should be implemented in a Nick-owned repository or a clearly separated
adapter directory with its own tests, license review, provenance, and release
process.

## Differentiation

The project should be memorable for correctness under messy real-world
conditions, not for a longer provider list.

- **Availability is evidence, not catalog presence.** A model is eligible only
  when current signals agree that it can serve the task.
- **Policy is structurally non-bypassable.** Ranking, memory, workflows, and
  model output operate only inside the candidate set produced by hard gates.
- **Swarming is a controlled execution primitive.** Every worker has a bounded
  task, proven runtime, isolated write target, termination rule, reviewer, and
  artifact lineage.
- **Learning starts in shadow mode.** Replay and rollback precede production
  influence.
- **Explanations are first-class output.** Operators can understand exclusions,
  uncertainty, fallbacks, and final choice without exposing credentials.
- **Claims ship with evidence.** Demos, benchmarks, package checks, failure
  fixtures, and exact-SHA workflows are part of the product.

## Integration principles

1. Depend only on documented public APIs and versioned contracts.
2. Pin and record integration versions; probe capabilities at runtime.
3. Treat missing or stale evidence as unknown, never as healthy.
4. Keep adapters optional so the deterministic core works without Hermes,
   OmniRoute, Ruflo, RuVector, or Hindsight.
5. Keep live provider state out of retained semantic memory.
6. Retain source attribution, observation time, version, trust label, and
   refresh rules with every knowledge record.
7. Run upstream marketing and performance claims through local reproduction
   before using them in product decisions or public copy.

## Roadmap

### Foundation

- Finish the current OmniRoute availability, capacity, planner, intelligence,
  retry, and explanation contracts.
- Freeze cross-language schemas and deterministic fixtures.
- Establish one routing authority and an adapter conformance suite.

### Bounded execution

- Add Ruflo integration behind a pinned capability manifest.
- Add worker eligibility, one-token plus exact-client canaries, isolated
  worktrees, bounded fan-out, backpressure, verifier roles, and provenance.
- Demonstrate pause, resume, cancel, partial failure, and deterministic stop.

### Advisory memory and learning

- Define redacted task, workflow, and outcome episode schemas.
- Add a RuVector adapter with source metadata and deterministic fallback.
- Benchmark retrieval, use observe-only ranking, then require replay, drift,
  rollback, and safety gates before any promotion.
- Keep Hindsight focused on human/operator continuation rather than runtime
  routing state.

### Flagship release

- Complete installer, wizard, quickstart, demo, benchmark harness, security
  assurance, package proof, support policy, examples, and visual assets.
- Reconcile Python and TypeScript behavior and document real gaps.
- Publish signed or attestable release artifacts only after clean-environment
  proof and terminal-green exact-SHA workflows.

### Portfolio release

- Repair and evidence the decision-systems repositories in dependency order.
- Replace static or self-referential badges with live evidence.
- Reconcile the profile last so every claim links to a reproducible artifact.

## Success measures

Product success is measured before popularity:

- clean-install and five-minute quickstart success rate;
- percentage of routing decisions with complete, redacted explanations;
- false-eligible and false-unavailable rates under fault injection;
- fallback correctness and bounded recovery time;
- verified cost/quality/latency changes against declared baselines;
- Python/TypeScript contract parity;
- time to resume an interrupted multi-repository effort;
- contributor time from issue to a reviewed, exact-SHA-green change;
- documentation tasks completed without maintainer clarification;
- security findings, regression escape rate, and rollback time.

Stars, forks, contributors, integrations, and downstream adoption are useful
lagging indicators, not release gates.

## Naming and brand decision

Do not rename the package in the middle of contract work. A name decision must
include:

- short, pronounceable, memorable language that does not imply unsafe
  autonomy;
- discoverable package, repository, and CLI names;
- trademark, domain, GitHub, PyPI, and npm availability checks;
- no confusion with an existing gateway, model router, or security product;
- a migration plan that preserves imports, CLI aliases, configuration, and
  links;
- a visual identity that works in terminals, diagrams, documentation, and
  small social previews.

Candidate generation and availability research belong in a separate,
time-bounded issue. The working description remains more important than the
working name.

## Non-goals

- Replacing OmniRoute's provider authentication and transport.
- Replacing Hermes as a user-facing durable agent.
- Forking Ruflo or RuVector into the core product.
- Guaranteeing that a model is good because an upstream catalog lists it.
- Letting semantic memory, a ranker, or an LLM override deterministic safety.
- Running two writers against one worktree or two Hermes containers against one
  data directory.
- Claiming production maturity, savings, throughput, or accuracy without
  reproducible evidence and limitations.

## Current decisions

- Keep primary Hermes orchestration host-native on the VPS; use Docker as a
  separate sandbox/reproducibility profile.
- Keep OmniRoute independently supervised and loopback-only.
- Keep `llm-gate` as the deterministic authority and all adaptive systems
  advisory until promoted by evidence.
- Store curated documentation syntheses and resume checkpoints in Hindsight;
  keep source documents at their authoritative locations.
- Treat Ruflo and RuVector as optional, pinned integrations.
- Treat the local `hermes-plugins` checkout as third-party.
- Complete the availability/capacity contract before naming and launch polish.

See the
[portfolio continuation runbook](PORTFOLIO_CONTINUATION_RUNBOOK.md) for the
execution sequence and the [memory source index](MEMORY_SOURCE_INDEX.md) for
the versioned knowledge-ingestion policy.
