# Execution Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete issue #53 with a truthful, privacy-safe, versioned execution-evidence explain surface while preserving legacy route responses.

**Architecture:** Keep the existing shared `RoutingDecisionContract` and `OutcomeEvent` as inner records. Add an evidence envelope and append-only in-memory lifecycle representation behind the existing store boundary; use server-owned opaque IDs and authenticated scope for lookup. The proxy records transport-observed facts only, and a stream wrapper owns exactly-once finalization plus upstream cleanup.

**Tech Stack:** Python 3.10+, FastAPI/Starlette, httpx, pytest/pytest-asyncio, JSON Schema Draft 2020-12, Code Review Graph, Ruff, mypy, Bandit, Hatch.

## Global Constraints

- Preserve the legacy `/v1/route` response shape; do not embed evidence in its JSON body.
- Do not persist prompts, completions, credentials, tool arguments, or inferred task correctness.
- Caller request/correlation IDs are metadata only; server-generated opaque evidence IDs are storage keys.
- Decision candidates are copied at creation time and never recomputed during lookup.
- Every stream termination path closes the upstream iterator and finalizes at most once.
- Transport success remains distinct from verification and quality; unobserved fields must say so.
- The current store remains explicitly process-local; durable SQLite/WAL is a separate ticket.
- Use `apply_patch` for edits, keep files under the repository’s existing size/style conventions, and do not touch unrelated worktree changes.

---

### Task 1: Freeze the evidence envelope and lifecycle model

**Files:**
- Modify: `verdict/evidence.py`
- Modify: `schemas/contracts.v1.json` (add the execution-evidence envelope definition without weakening existing inner contracts)
- Test: `tests/test_evidence.py`

**Interfaces:**
- `ExplainEvidence` gains an immutable ordered `events` collection while retaining `outcome_event` as the current/latest-event compatibility field.
- `ExplainEvidence.to_dict()` emits `kind: "execution_evidence"`, `envelope_version: "1"`, `routing_decision`, `events`, `outcome_event`, and optional `evidence_id`/`scope`.
- `EvidenceStore.put()` returns a server-owned key; `EvidenceStore.append_event(key, event)` appends a non-terminal event or accepts the first terminal event and returns the current record.
- `EvidenceStore.update_outcome()` remains as a compatibility alias delegating to `append_event()`.

- [ ] **Step 1: Write failing lifecycle/schema tests**

Add tests that create a record, append `execution_started` then a terminal event, and assert both events remain in order, the latest event is exposed as `outcome_event`, and a late terminal event does not replace the first terminal event. Validate the envelope against a new `execution_evidence` schema definition.

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `uv run pytest -q tests/test_evidence.py`

Expected: failures for the missing `events` field, envelope discriminator, schema definition, and `append_event` method.

- [ ] **Step 3: Implement the minimal immutable envelope/store changes**

Copy the event tuple whenever constructing `ExplainEvidence`; have `put()` seed it with the initial event; have `append_event()` preserve the tuple and reject replacement after a terminal outcome. Keep scope checks and bounded eviction unchanged.

- [ ] **Step 4: Run the focused tests to verify the task passes**

Run: `uv run pytest -q tests/test_evidence.py`

Expected: all evidence tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add verdict/evidence.py schemas/contracts.v1.json tests/test_evidence.py
git commit -m "feat(evidence): preserve append-only execution lifecycle"
```

### Task 2: Make the explain API tagged and backward-compatible

**Files:**
- Modify: `verdict/api.py`
- Modify: `tests/test_proxy.py`
- Modify: `tests/integration/test_live_gateway.py` only if an existing assertion needs an explicit compatibility check

**Interfaces:**
- `/v1/route` returns only the legacy decision fields and may expose an opaque evidence ID through headers.
- `/v1/route/explain?evidence_id=...`, `request_id=...`, or `correlation_id=...` returns the tagged execution-evidence envelope.
- Availability explain responses include `kind: "availability_explain"` and remain selected by `model_id`/no selector.
- Conflicting selectors return HTTP 400; ambiguous caller-ID selectors return HTTP 409; missing/scope-mismatched evidence returns HTTP 404.

- [ ] **Step 1: Write failing API compatibility/tagging tests**

Assert `/v1/route` has no `evidence` body member, route headers contain the opaque ID when evidence is retained, evidence explain responses have the execution discriminator and ordered events, and availability explain responses have the availability discriminator.

- [ ] **Step 2: Run the focused API tests to verify failure**

Run: `uv run pytest -q tests/test_proxy.py tests/integration/test_live_gateway.py`

Expected: failures because route currently embeds evidence and explain responses lack discriminators.

- [ ] **Step 3: Implement compatibility-preserving API behavior**

Remove body embedding from `route_task()`, add response headers where a response object is available, and add explicit `kind` fields to both explain branches without changing their existing data fields.

- [ ] **Step 4: Run the focused API tests to verify the task passes**

Run: `uv run pytest -q tests/test_proxy.py tests/integration/test_live_gateway.py`

Expected: all focused API tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add verdict/api.py tests/test_proxy.py tests/integration/test_live_gateway.py
git commit -m "fix(api): keep route compatibility and tag explain responses"
```

### Task 3: Harden stream finalization and upstream cleanup

**Files:**
- Modify: `verdict/api.py`
- Modify: `tests/test_proxy.py`

**Interfaces:**
- Add a private async stream adapter whose `__anext__()` forwards chunks and whose `aclose()` finalizes a cancellation/abort event exactly once before closing the upstream iterator.
- Normal exhaustion records `streaming_phase: "completed"`; cancellation, explicit close, disconnect, and iterator errors record terminal aborted/error events.
- Cleanup is safe when the upstream iterator has no `aclose()` method.

- [ ] **Step 1: Write failing stream lifecycle tests**

Add a close-counting async stream and tests for partial consumption followed by `await body_iterator.aclose()`, a mid-stream exception, cancellation, and repeated close. Assert one terminal event, the expected phase/outcome, and exactly one upstream close.

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `uv run pytest -q tests/test_proxy.py -k 'stream or cancellation or abort'`

Expected: failures for explicit close/finalization and close-count assertions.

- [ ] **Step 3: Implement the private stream adapter**

Replace the nested async-generator-only wrapper with an object that owns terminalization state, handles `CancelledError`, `GeneratorExit`, ordinary exceptions, normal exhaustion, and explicit `aclose()`, and invokes the upstream close path from one idempotent cleanup method.

- [ ] **Step 4: Run the focused stream tests to verify the task passes**

Run: `uv run pytest -q tests/test_proxy.py -k 'stream or cancellation or abort'`

Expected: all stream lifecycle tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add verdict/api.py tests/test_proxy.py
git commit -m "fix(proxy): finalize evidence on every stream termination"
```

### Task 4: Make runtime evidence claims explicitly truthful

**Files:**
- Modify: `verdict/evidence.py`
- Modify: `tests/test_evidence.py`
- Modify: `tests/fixtures/evidence-cases.json`

**Interfaces:**
- Transport-observed outcome fields use `verification.status: "not_observed"` and `quality.outcome: "not_observed"` unless an authoritative verifier supplies stronger data.
- Cost, retries, fallbacks, provider version, and model version remain empty/null when not observed.
- The outcome details include protocol metadata and transport status, never response bodies or task content.

- [ ] **Step 1: Write failing truthfulness assertions**

Assert runtime-built outcome events do not use `success` as verification, do not fabricate cost/retry/fallback/version data, and preserve the distinction between `outcome: "success"` and verification `not_observed`.

- [ ] **Step 2: Run the focused tests to verify failure**

Run: `uv run pytest -q tests/test_evidence.py tests/test_proxy.py -k 'evidence or buffered or stream'`

Expected: failures on the current `not_evaluated`/`unknown` labels.

- [ ] **Step 3: Implement explicit unobserved markers and redaction assertions**

Change only the evidence factory labels and deterministic fixtures; do not add inferred quality or cost calculations to the proxy.

- [ ] **Step 4: Run the focused tests to verify the task passes**

Run: `uv run pytest -q tests/test_evidence.py tests/test_proxy.py -k 'evidence or buffered or stream'`

Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add verdict/evidence.py tests/test_evidence.py tests/fixtures/evidence-cases.json
git commit -m "fix(evidence): label unobserved verification truthfully"
```

### Task 5: Run repository-wide verification and ADR compliance review

**Files:**
- Modify: only files required to fix verification findings in Tasks 1–4

- [ ] **Step 1: Rebuild Code Review Graph and inspect impact**

Run the Code Review Graph full rebuild, `detect_changes` against `origin/main`, affected-flow analysis, and review-context generation for all changed source/test/schema files. Confirm no parse errors and inspect high-risk flows.

- [ ] **Step 2: Run the complete local verification matrix**

```bash
uv run pytest -q
uv run --extra dev --extra dashboard --extra server ruff check .
uv run --extra dev --extra dashboard --extra server ruff format --check .
uv run --extra dev --extra dashboard --extra server mypy verdict --strict
uv run python -m build
uv run python -m twine check dist/*
uv run bandit -q -r verdict
git diff --check
```

Record exact exit status and any pre-existing findings separately from new findings.

- [ ] **Step 3: Run ADR review against accepted decisions**

Check the diff against `docs/architecture/ADR-ORCHESTRATOR-ROUTING.md`, `docs/architecture/ADR-001-evidence-ledger.md`, and the referenced Ruflo provenance/safety patterns. Any violation must be fixed or documented as a deliberate superseding decision.

- [ ] **Step 4: Request independent review**

Provide the reviewer the base SHA, final implementation SHA, issue #53 acceptance criteria, design spec, and verification output. Resolve all critical/important findings before PR creation.

- [ ] **Step 5: Commit verification repairs**

```bash
git add verdict/evidence.py verdict/api.py schemas/contracts.v1.json tests/test_evidence.py tests/test_proxy.py tests/integration/test_live_gateway.py
git commit -m "test(evidence): close issue 53 verification gaps"
```

### Task 6: Push, gate, and merge the exact reviewed head

**Files:**
- No source changes unless CI exposes a new verified failure.

- [ ] **Step 1: Verify clean intent and exact diff**

Run `git status --short`, `git diff origin/main...HEAD --stat`, `git diff origin/main...HEAD --check`, and record `git rev-parse HEAD`.

- [ ] **Step 2: Push and open the issue-linked PR**

```bash
git push -u origin codex/issue-53-explain-evidence
gh pr create --base main --head codex/issue-53-explain-evidence --title "feat(evidence): explain routing and transport outcomes" --body "Closes #53. Adds versioned privacy-safe execution evidence, truthful transport outcomes, and stream lifecycle coverage. Process-local storage limitations and the durable-ledger follow-up are documented in docs/architecture/ADR-EVIDENCE-LEDGER.md."
```

The PR body must link `#53`, list acceptance evidence, state process-local limitations, and identify the durable-ledger follow-up.

- [ ] **Step 3: Monitor exact-head CI and review state**

Use `gh pr checks <number> --watch` and `gh pr view <number> --json headRefOid,statusCheckRollup,reviews,mergeable`. Do not accept a green check from a different head SHA.

- [ ] **Step 4: Repair failures on the same branch**

For every failure, reproduce locally, patch only the relevant files, rerun the full affected matrix, request a fresh review, and verify that the reviewed SHA equals the PR head SHA. Conflict repair invalidates prior review and CI evidence.

- [ ] **Step 5: Merge and verify main**

Merge only after required checks and review are green. Then verify the merge SHA is present on `origin/main`, the PR is merged (not merely closed), and no superseded open/conflicting PR remains for issue #53.
