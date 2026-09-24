# Story 269 specification

## Problem

The public README does not provide one credential-free, copy-pasteable journey
from installation verification through provider inspection, route selection, a
bounded mission, forced failover, and replay. Existing documentation smoke
coverage checks strings instead of executing the documented CLI contracts, and
the maturity claims do not consistently distinguish local proof from live
provider evidence.

## Goal

Center the README on the install → provider → route → mission → failover →
replay journey. Every command in that journey must be executable in CI without
credentials or external network access, and the maturity matrix must state
what is functional, incomplete, simulated, or missing.

## User stories

- As a new user, I can verify the installed CLI and inspect provider signals
  without configuring credentials.
- As an evaluator, I can run a deterministic route decision and bounded mission
  against local fixtures.
- As an operator, I can force a transient failure, observe the replacement
  route, and reload the persisted execution session.
- As a reviewer, I can map every documented offline command to an automated
  smoke assertion and understand the limits of each proof.

## Functional requirements

1. README presents these stages in order: install, provider, route, mission,
   failover, replay.
2. README links to a canonical `docs/USER_JOURNEY.md` containing command
   details, expected evidence, and limitations.
3. The documented offline command set is:
   - `verdict --help`
   - `verdict detect --offline --json`
   - `verdict quickstart --non-interactive --dry-run --json`
   - `verdict autodev-golden-path ... --json`
   - `verdict failover-proof ... --json`
   - `VERDICT_MEMORY_DB=... verdict replay <session-id> --json`
4. `detect --offline` must not open sockets, invoke provider CLIs, read API
   credentials, or make HTTP requests. Its JSON must explicitly identify the
   result as offline inspection.
5. `failover-proof` must expose the existing deterministic forced-HTTP-429 proof
   through the CLI, persist its session to a caller-selected MemoryPlane, return
   the replayable session id in JSON, and be repeatable against the same
   database path.
6. Documentation smoke tests must execute every command contract above in an
   isolated temporary environment and must not require credentials, Ruflo,
   OmniRoute routing, autopilot, or another worktree.
7. The maturity matrix must use only these statuses:
   `production functional`, `functional but incomplete`, `simulated only`, or
   `missing`, with evidence and limitations on every row.

## Non-functional requirements

- Preserve all existing live detection and routing behavior unless the caller
  explicitly selects the new offline mode.
- Do not add dependencies or modify credentials, global configuration, Ruflo,
  OmniRoute task routing, autopilot, or manifests owned by other stories.
- Keep emitted evidence deterministic, bounded, and credential-safe.
- Support Python 3.10+ and the repository's existing Ruff and strict mypy
  configuration.

## Acceptance criteria

1. README is rewritten around install → provider → route → mission → failover
   → replay.
2. Every documented journey command is exercised by a CI-runnable smoke test.
3. The maturity matrix is updated with the four required status categories.
4. Focused CLI, failover/replay, golden-path, and documentation tests pass.

## Non-goals

This story does not enable live task routing, prove provider quality or
availability, generate code with an LLM, change provider credentials, or claim
that simulated failover establishes live-provider reliability.
