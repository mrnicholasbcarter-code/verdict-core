# Story 269 User Journey Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a truthful, credential-free README journey whose six CLI stages are executed by CI-runnable smoke tests.

**Architecture:** Preserve existing command behavior and expose only two narrow proof seams: pure offline provider inspection and the existing forced-failover proof. A subprocess smoke test drives the public CLI end-to-end in an isolated temporary environment, then README and the canonical journey page describe only those verified contracts.

**Tech Stack:** Python 3.10+, argparse CLI, pytest, MemoryPlane/ExecutionSession, Markdown.

## Global Constraints

- Work only in `/home/nick/dev/verdict-core/verdict-core-v1-269` on `feat/v1-269`.
- Do not modify shared main, other worktrees, credentials, Ruflo, OmniRoute task routing, autopilot, or other-story manifests.
- Preserve default live detection and routing behavior.
- Add no dependencies; commit only after verification and independent review are complete.
- Every offline README journey command must have fresh focused test evidence.

---

### Task 1: Offline provider inspection

**Files:**
- Modify: `verdict/cli.py`
- Modify: `tests/test_documentation_smoke.py`

**Interfaces:**
- Consumes: existing `cmd_detect(output_json, verbose)` dispatch.
- Produces: `cmd_detect(output_json, verbose, offline=False)` and CLI flag `detect --offline`.

- [ ] Add a failing test asserting offline detection reports no network or credential access.
- [ ] Run the focused test and confirm failure before implementation.
- [ ] Add an early deterministic offline payload in `cmd_detect` and register `--offline`.
- [ ] Run the focused test and confirm it passes.

### Task 2: CLI failover proof

**Files:**
- Modify: `verdict/cli.py`
- Modify: `tests/test_documentation_smoke.py`

**Interfaces:**
- Consumes: `run_forced_failover_proof(MemoryPlane) -> ReplayProof`.
- Produces: `cmd_failover_proof(memory_path: str, output_json: bool) -> None` and `verdict failover-proof --memory-path PATH --json`.

- [ ] Add a failing subprocess test asserting a forced 429, replacement model, and replayable session id.
- [ ] Run the focused test and confirm failure before implementation.
- [ ] Add the thin CLI wrapper, parser registration, and dispatch.
- [ ] Run the focused test and confirm it passes.

### Task 3: End-to-end journey smoke

**Files:**
- Modify: `tests/test_documentation_smoke.py`

**Interfaces:**
- Consumes: all six public journey commands.
- Produces: one isolated smoke test that validates help, offline detection, fixture route, golden path, failover, and replay.

- [ ] Build a temporary Git repository and isolated CLI environment.
- [ ] Execute every command through the current Python environment's
  `python -m verdict.cli` entry point, which exercises the same parser and
  dispatch without depending on an unrelated `verdict` executable on `PATH`.
- [ ] Assert bounded JSON evidence and replay continuity for every stage.
- [ ] Run the complete documentation smoke module.

### Task 4: README and maturity documentation

**Files:**
- Modify: `README.md`
- Modify: `docs/USER_JOURNEY.md`
- Modify: `tests/test_documentation_smoke.py`

**Interfaces:**
- Consumes: verified commands and payloads from Tasks 1–3.
- Produces: the public six-stage journey and four-state maturity matrix.

- [ ] Rewrite the README opening path around the six stages and link the canonical journey.
- [ ] Document exact offline commands, expected evidence, and limitations.
- [ ] Add a test mapping the documented commands to the executed smoke manifest.
- [ ] Remove or qualify stale availability, provider-count, and production claims in touched sections.

### Task 5: Verification and impact review

**Files:**
- Modify: `docs/specs/story-269/tasks.md`

**Interfaces:**
- Consumes: final worktree diff and updated code graph.
- Produces: fresh test/lint evidence and a completed task checklist.

- [ ] Run focused documentation, CLI, golden-path, and failover/replay tests.
- [ ] Run Ruff on changed Python files.
- [ ] Rebuild/update the code graph and inspect changed-file impact.
- [ ] Review `git diff`, confirm only Story 269 files changed, update the
  checklist, and commit after independent review.
