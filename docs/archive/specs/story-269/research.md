# Story 269 research

## Acceptance source

GitHub issue #269 requires:

- README rewritten around install → provider → route → mission → failover →
  replay.
- Every README journey command exercised by a CI-runnable smoke test.
- An updated maturity matrix using truthful implementation states.

The issue references `evidence/V1_READINESS_AUDIT_2026-08-03.md`; the story is a
documentation and smoke-proof slice, not an authorization to change the live
control plane.

## Existing implementation seams

- `verdict --help` exposes the CLI registration contract.
- `verdict detect --json` performs environment discovery, but its normal path
  may inspect local ports, provider CLIs, and configured API-key environment
  names. A deterministic offline mode is needed for the README proof.
- `verdict quickstart --non-interactive --dry-run --json` already returns a
  credential-free fixture decision with one selected route and explicit
  exclusions.
- `verdict autodev-golden-path` already runs discovery, durable memory, and a
  bounded verification command against a real local Git repository. It does
  not call an LLM or edit the repository.
- `verdict.failover_replay_proof.run_forced_failover_proof()` already records a
  forced HTTP 429, selects an eligible replacement, avoids duplicate commits,
  and persists a replayable `ExecutionSession`. It lacks a CLI entry point.
- `verdict replay` reads an execution session from `VERDICT_MEMORY_DB`; the
  failover proof can provide the required session id and database.

## Test findings

- Existing `tests/test_documentation_smoke.py` only checked command strings and
  stale marketing claims.
- Existing focused tests cover quickstart, provider detection, golden path,
  execution sessions, and failover/replay internals, so Story 269 can add a
  thin end-to-end documentation smoke suite without inventing new runtime
  abstractions.

## Decisions

1. Add `detect --offline` as a pure, deterministic inspection mode while
   preserving the default discovery behavior.
2. Add `failover-proof --memory-path PATH --json` as a thin CLI wrapper over the
   existing proof module.
3. Use subprocess CLI execution in the documentation smoke test so argument
   registration, dispatch, exit codes, JSON serialization, persistence, and
   replay are all covered.
4. Keep install proof to `verdict --help` inside the installed CI environment;
   cloning, remote installers, and credentials are intentionally outside the
   offline smoke path.

## Risks and mitigations

- **Environment leakage:** strip provider credential variables and set an
  isolated `HOME` in subprocess tests.
- **Network leakage:** offline detection returns before normal discovery and
  tests monkeypatch socket/subprocess seams in focused unit coverage.
- **Replay mismatch:** use the exact memory path emitted by the failover proof
  and pass it through `VERDICT_MEMORY_DB`.
- **Overclaiming maturity:** label fixture routing and forced failures as
  `simulated only`; label live integration surfaces as `functional but
  incomplete`.
