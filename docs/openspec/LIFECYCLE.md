# OpenSpec Lifecycle Integration

**Status:** Active
**Schema:** verdict-change-v1
**OpenSpec Version:** 1.13.2 (pinned via `npx -y @fission-ai/openspec@1.13.2`)

## Overview

Verdict integrates OpenSpec as a first-class, durable input to the autonomous delivery lifecycle **without moving authority** away from Linear, Git/GitHub, or Verdict.

For significant changes, the runner:
1. Resolves a Linear issue to one OpenSpec change
2. Validates the change artifacts (FAILS CLOSED on missing/invalid/PLACEHOLDER content)
3. Snapshots the OpenSpec revision digest into durable run state
4. Executes bounded tasks
5. Verifies spec conformance + Verdict proof
6. Archives only after merged-main verification

## Authority Model

| Authority | Responsibility |
|-----------|---------------|
| **Linear** | Queue, priority, blockers, lifecycle intent |
| **OpenSpec** | Structured change contract: proposal/specs/design/tasks |
| **Verdict** | Plan admission, execution, policy, recovery, VERIFY, review, receipt |
| **Prime** | Execution harness (NOT an OpenSpec authority) |
| **Git/GitHub** | Source/branch/PR/CI/merge truth |
| **Receipts** | What actually happened (certification evidence) |

**Critical:** `openspec verify` is spec-conformance evidence ONLY. It never marks a story Done and never replaces Verdict proof or exact-head/merged-main verification.

## Lifecycle Diagram

```text
Linear BOD-id
  ↓
OpenSpec change id (deterministic mapping: BOD-205 → bod-205-*)
  ↓
Validate artifacts (FAILS CLOSED: missing/invalid/PLACEHOLDER)
  ↓
Persist spec revision digest in run state
  ↓
Verdict UNDERSTAND/PLAN/HYDRATE
  ↓
Story-owned worktree/branch
  ↓
Task execution
  ↓
OpenSpec verify (conformance evidence)
  ↓
Verdict VERIFY (proof + exact-head CI)
  ↓
Independent review
  ↓
PR exact-head CI → merge
  ↓
Merged-main verification
  ↓
Receipt/certification (includes OpenSpec block)
  ↓
OpenSpec archive (ONLY after merged-main verification)
  ↓
Linear Done (manual or automated)
```

## Commands

### Admission

```bash
# Validate and admit a significant change
verdict openspec admit <change-id> [--repo PATH] [--json]

# Examples
verdict openspec admit bod-205
verdict openspec admit BOD-205  # Linear issue ID auto-converted
verdict openspec admit bod-205-openspec-lifecycle --json
```

**Exit codes:**
- `0`: Admitted
- `1`: Blocked (validation failed or PLACEHOLDER content)

**Structured output** (with `--json`):
```json
{
  "admitted": true,
  "reason": "Change validated and admitted",
  "category": "ok",
  "change_id": "bod-205-openspec-lifecycle"
}
```

### Loading Changes

Verdict prefers **structured OpenSpec CLI output** over fragile Markdown parsing:

```bash
# Structured data (preferred)
npx -y @fission-ai/openspec@1.13.2 change show <change-id> --json --no-interactive

# Validation
npx -y @fission-ai/openspec@1.13.2 change validate <change-id> --json --strict
```

Falls back to reading files directly ONLY for fields with no structured output.

## Failure Behaviors

| Condition | Behavior |
|-----------|----------|
| OpenSpec unavailable/malformed for significant story | **BLOCK** with explicit reason; do not silently execute from stale chat/prose |
| Spec revision changed mid-run | Pause/replan affected tasks and proof; set `spec_changed` flag |
| Required section is PLACEHOLDER-only (TBD, N/A, TODO, -, ..., none) | **BLOCK** admission |
| OpenSpec verification fails | Story remains incomplete |
| Verdict proof/CI/review fails after OpenSpec verify | Story remains incomplete |
| Worker/provider failure | Isolated from root and unrelated stories |

## Durable Run State

The run state (graph.json) includes an optional `openspec` block:

```json
{
  "run_id": "...",
  "goal": "...",
  "nodes": [...],
  "openspec": {
    "linear_issue": "BOD-205",
    "openspec_change": "bod-205-openspec-lifecycle",
    "openspec_schema": "verdict-change-v1",
    "spec_revision_digest": "abc123...",
    "current_task": null,
    "completed_tasks": [],
    "conformance_result": null
  }
}
```

### Resume Behavior

On **resume**, the runner:
1. Recomputes the spec revision digest from the current change directory
2. Compares it to the persisted digest
3. If changed: marks the run `spec_changed` and requires re-plan (does NOT continue blindly)
4. If unchanged: resumes normally

### Receipt

The receipt (receipt.json) records the `openspec` block, preserving:
- Linear issue and OpenSpec change IDs
- Schema version
- Spec revision digest at run start
- Conformance result (from `openspec verify`)

This creates an immutable audit trail linking Linear intent → OpenSpec contract → execution → verification evidence.

## Archive Preconditions

`openspec archive` is allowed **ONLY** when:
- Merged-main verification evidence is present
- Verdict proof passed
- Exact-head CI passed
- Review approved

The archive call is a thin wrapper around the CLI. The precondition check is enforced in `verdict.openspec_lifecycle.can_archive_openspec_change()`.

## Out of Scope (Deferred)

- Runner auto-dispatch integration (manual for now)
- Linear status writes (read-only Linear binding)
- Full bidirectional sync
- `verdict-core openspec init` (use ecosystem `openspec init --tools none` directly for now)

## Telemetry

All `openspec` subprocess calls **MUST** set `OPENSPEC_TELEMETRY=0` in the environment to disable telemetry. This is enforced in `_run_openspec_command()` and tested in unit tests.

## Implementation

- **verdict/openspec_lifecycle.py**: Typed, mypy --strict clean, core lifecycle functions
- **verdict/openspec_vendor/**: Byte-identical vendored validator from verdict-ecosystem@fec5556
- **verdict/commands/parsers_openspec.py**: CLI parsers
- **tests/test_openspec_*.py**: Hermetic unit tests + optional integration test
- **tests/fixtures/openspec/**: Valid and invalid test fixtures

## Dependencies

- OpenSpec 1.13.2 (pinned, global or npx)
- verdict-ecosystem@fec5556 (reference schema and validator)
- Python 3.10+
