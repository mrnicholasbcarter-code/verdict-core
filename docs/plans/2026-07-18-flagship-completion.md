# Flagship Completion Execution Plan

> Execution authority: the repository issues, the July 17-18 Hermes sessions,
> and the operator's instruction to complete the portfolio with bounded,
> dynamically selected OmniRoute workers.

## Outcome

Finish `llm-gate` as the portfolio flagship, bring `llm-gate-node` to an honest
parity boundary, and then verify and polish the remaining portfolio repositories.
No model, provider, or capability tier is treated as a permanent product fact.
Runtime candidates come from the configured catalog and must pass deterministic
requirements and live availability checks before ranking.

## Worker operating rules

1. The main agent owns architecture, security, release decisions, integration,
   and final verification.
2. A worker receives one bounded task in an isolated worktree.
3. Immediately before assignment, the selected runtime model must pass:
   - catalog capability inspection;
   - a fixed one-token, no-tool OpenAI-compatible probe with non-empty assistant
     output and positive usage;
   - the exact `omniroute launch-codex -- exec` path used for the assignment.
4. Failed or ambiguous models are excluded for that run. Model identifiers are
   runtime data and are never committed as product policy.
5. At most two implementation workers run concurrently on this VPS.
6. Every implementation gets a separate requirements review, code-quality
   review, and main-agent verification before integration.

## Wave 1: make live availability truthful

### Task 1: harden the probe contract

Files:

- Modify `tests/test_probes.py`
- Modify `llm_gate/probes.py`
- Modify `README.md`

Steps:

1. Add failing tests proving that zero usage, empty assistant output, quota
   exhaustion, and HTTP transport errors cannot produce `ready`.
2. Run `pytest -q tests/test_probes.py` and record the expected failures.
3. Require a successful status, positive usage, and non-empty assistant output.
4. Preserve HTTP status classification through the stdlib transport.
5. Make injected observation time deterministic.
6. Run focused tests, then the complete quality suite.
7. Commit this pre-existing Hermes slice as one reviewed atomic change.

### Task 2: connect probes to catalog eligibility

Files:

- Modify `llm_gate/availability.py`
- Modify `llm_gate/api.py`
- Add or modify focused tests under `tests/`

Steps:

1. Specify the public boundary between catalog rows, probe observations, and
   normalized candidates.
2. Add failing tests for stale, unknown, degraded, denied, and ready states.
3. Ensure a catalog row alone never establishes readiness.
4. Expose redacted availability reasons in model and route explanations.
5. Verify that allow/deny policy and task requirements run before ranking.

## Wave 2: replace legacy tier routing

### Task 3: define minimal-sufficient capability scoring

Files:

- Modify `llm_gate/contracts.py`
- Modify `llm_gate/models.py`
- Modify `llm_gate/classifier.py`
- Modify `llm_gate/intelligence.py`
- Modify `llm_gate/dispatcher.py`
- Modify their focused tests

Steps:

1. Add characterization tests for every supported public routing path.
2. Express task requirements as capabilities, privacy, context, tool, latency,
   budget, and production-impact constraints.
3. Filter all ineligible candidates before any adaptive ranking.
4. Rank remaining candidates by sufficient capability and operator objective,
   preferring lower cost/strength when quality requirements are still met.
5. Remove required tier fields and hard-coded model-family policy from active
   routing contracts through a documented compatibility migration.
6. Prove that adaptive guidance cannot reintroduce an ineligible candidate.

### Task 4: production intelligence boundary

Files:

- Modify `llm_gate/intelligence.py`
- Modify `llm_gate/planner.py`
- Modify `llm_gate/api.py`
- Add protocol and failure-mode tests

Steps:

1. Implement the versioned intelligence contract from
   `docs/specs/INTELLIGENCE_ADAPTER_PROTOCOL_V1.md`.
2. Separate deterministic readiness from managed Ruflo/RuVector readiness.
3. Fail closed in production when mandatory intelligence is unavailable.
4. Allow deterministic degraded mode only when explicitly configured.
5. Preserve correlation IDs and redacted, structured failure envelopes.

## Wave 3: legal execution semantics

### Task 5: retry, fallback, and streaming safety

Files:

- Modify `llm_gate/api.py`
- Add retry-policy modules only if the existing boundary cannot stay cohesive
- Add focused mock-upstream and streaming tests

Steps:

1. Encode idempotency and retry eligibility explicitly.
2. Distinguish auth, quota, rate-limit, overload, timeout, transport, and
   circuit-open failures.
3. Honor bounded backoff and `Retry-After`.
4. Re-run eligibility filtering before every fallback.
5. Prohibit fallback once response bytes have been streamed.
6. Emit redacted attempt evidence.

### Task 6: bounded swarm dispatcher

Files:

- Modify `llm_gate/dispatcher.py`
- Modify `llm_gate/planner.py`
- Add dispatcher integration tests

Steps:

1. Convert a validated workflow plan into dependency-aware assignments.
2. Enforce concurrency, retry, time, and cost budgets.
3. Require verifier evidence before promoting worker output.
4. Escalate failed or ambiguous work to the main agent.
5. Support a no-side-effect dry-run explanation path.

## Wave 4: release proof and Node parity

### Task 7: Python release evidence

Files:

- Modify `scripts/`, `.github/workflows/`, package metadata, and release docs as
  required by `docs/specs/RELEASE_ACCEPTANCE.md`

Steps:

1. Build wheel and sdist.
2. Install each artifact in a clean virtual environment.
3. Run import, CLI, raw HTTP, OpenAI-client, mock-upstream, and filtered
   OmniRoute smoke tests.
4. Run unit/integration tests, Ruff, type checking, dependency audit, Bandit,
   package inspection, and benchmark fixtures.
5. Reconcile every README and release-matrix claim with captured evidence.

### Task 8: `llm-gate-node` parity

Files:

- Modify only the Node repository in its own worktree.

Steps:

1. Compare issue acceptance criteria with forwarding/SSE and package-boundary
   behavior already on `master`.
2. Add failing parity tests for any real gap.
3. Fix only verified gaps; keep unsupported behavior explicit.
4. Run tests, type checking, build, package dry-run, and clean-install smoke.

## Wave 5: portfolio completion

For `backtest-harness`, `edge-mining-framework`, `hermes-plugins`,
`trade-risk-engine`, `trading-cockpit-ui`, and the profile repository:

1. Audit the repository against its README, open issues, package metadata, CI,
   security posture, and reproducibility claims.
2. Create one bounded plan per repository.
3. Implement independent fixes in worktrees with the worker protocol above.
4. Run each repository's complete local verification.
5. Integrate atomic commits, push them, and confirm remote CI.
6. Finish with a cross-repository portfolio review and an evidence-backed
   release/readiness report.
