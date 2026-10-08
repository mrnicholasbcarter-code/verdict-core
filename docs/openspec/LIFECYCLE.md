# OpenSpec Lifecycle Integration

**Status:** Active\
**Schema:** verdict-change-v1\
**OpenSpec Version:** 1.13.2 (pinned via `npx -y @fission-ai/openspec@1.13.2`)

## Overview

Verdict supports an optional OpenSpec binding for a local orchestration run. This binding does **not** move authority away from Linear, Git/GitHub, or Verdict. The operator supplies an exact, full change-directory slug with `--openspec-change`; the runner does not fetch a Linear issue or search for a matching slug.

For a bound run, the runner validates on-disk change artifacts with the vendored validator and a limited placeholder check before work, saves a revision digest in durable run state, then executes bounded tasks. After execution, pinned OpenSpec strict CLI validation checks structural conformance before the final **local run receipt verdict**. Unavailable or malformed CLI conformance evidence blocks that final completion. The runner does not fetch or update Linear, check exact-head CI or merged main, merge a PR, archive a change, or produce CERTIFIED release evidence. Those remain external/operator-owned steps.

## Authority Model

| Authority | Responsibility |
|-----------|---------------|
| **Linear** | Queue, priority, blockers, lifecycle intent; external issue lookup and Done transitions |
| **OpenSpec** | Structured change contract: proposal/specs/design/tasks |
| **Verdict** | Local admission, execution, policy, recovery, VERIFY, review, conformance gate, run receipt |
| **Prime** | Execution harness (NOT an OpenSpec authority) |
| **Git/GitHub** | Source/branch/PR/CI/merge truth, checked outside this OpenSpec binding |
| **Run receipts** | Recorded local events and verdict; not CERTIFIED release evidence or external CI/merge proof |

**Critical:** pinned `openspec validate <change-id> --strict --json` is structural spec-conformance evidence ONLY. It does not independently prove implementation correctness, mark a story Done, or replace Verdict proof or external exact-head/merged-main verification.

## Lifecycle Diagram

The first box is operator input. The middle boxes are implemented in a bound local run; the final boxes describe a target release workflow **outside** the OpenSpec runner.

```text
OPERATOR: choose full OpenSpec change slug from Linear intent (no Linear fetch/slug search)
  ↓
VERDICT: validate on-disk artifacts (vendored structural checks + limited placeholders)
  ↓
VERDICT: persist change revision digest in graph.json; compare on resume
  ↓
VERDICT: plan and run bounded tasks → local verification and independent review
  ↓
VERDICT: pinned OpenSpec strict CLI validation before final local receipt verdict
  ↓
VERDICT: local run receipt with OpenSpec block (not CERTIFIED release evidence)
  ↓
EXTERNAL/OPERATOR: PR exact-head CI → merge → verify merged main
  ↓
EXTERNAL/OPERATOR: archive OpenSpec change; move Linear issue to Done
```

Local strict validation checks the change structure. Neither it nor the local receipt proves the external CI, merge, or Done steps occurred.

## Commands

### Admission

```bash
# Use the exact full directory slug, not an issue ID or prefix
verdict openspec admit <full-change-slug> [--repo PATH] [--json]

# Example (repo/openspec/changes/bod-205-openspec-lifecycle/ must exist)
verdict openspec admit bod-205-openspec-lifecycle --json
```

The CLI lowercases an input beginning with `BOD-` (for example `BOD-205` becomes `bod-205`) but does not resolve a prefix to `bod-205-openspec-lifecycle`. `load_change` uses that exact ID to find a directory; a prefix or issue ID is not a working alias for the full slug. It reads structured CLI `change show` JSON for standalone admission. Malformed but syntactically valid JSON (such as a list or `null`) or a file read error can raise instead of returning a structured blocked reason; do not rely on `--json` for a graceful failure in those cases.

**Exit codes (when the command returns normally):**
- `0`: Admitted
- `1`: Blocked (for example, missing change, vendored validation failure, or detected placeholder content)

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

Standalone `verdict openspec admit` first asks the pinned CLI for structured `change show` JSON, then reads change artifacts from the exact on-disk directory. Bound `verdict orchestrate --openspec-change` builds the change from the supplied directory directly and uses the vendored local validator at pre-work admission; it does not require `change show` or `--version` at that point. The pinned CLI is required later for strict conformance before a final local COMPLETE verdict:

```bash
# Structured data (preferred)
npx -y @fission-ai/openspec@1.13.2 change show <change-id> --json --no-interactive

# Validation
npx -y @fission-ai/openspec@1.13.2 change validate <change-id> --json --strict
```

The loader reads proposal/design/tasks/spec files to populate its artifacts. The orchestrator instead binds the change directory without calling the `change show` loader.

## Failure Behaviors

| Condition | Behavior |
|-----------|----------|
| Bound change directory missing or vendored local checks fail | Pre-work admission blocks; the CLI need not be present yet |
| Pinned OpenSpec CLI missing/timed out at final conformance, invalid conformance JSON/schema, or strict validation fails | Final local completion blocks (or fails for reported validation issues), after work may have run |
| Standalone `admit`: `change show` unavailable/nonzero/invalid JSON | Change load returns no change; admission blocks. A non-mapping JSON value or artifact read error may instead raise without a structured blocked result |
| Spec revision changed on resume or during final conformance | **BLOCK** with `spec_changed`; a new/replanned run is required |
| Placeholder-only content in specifically checked proposal/design headings, requirement bodies, or all task checkboxes | **BLOCK** admission where detected; see limited coverage below |
| Other required design sections with placeholder-only body | May pass vendored nonblank-body validation; no universal placeholder rejection exists |
| Verdict integration/proof/review fails | Final local completion cannot be claimed; external exact-head CI is not checked here |
| Worker/provider failure | Bounded recovery applies; not an OpenSpec archive or Linear status operation |

The core placeholder pass checks proposal **Why**, **What Changes**, **Impact**; design **Context**, **Goals / Non-Goals**, **Decisions**; requirement bodies in `specs/**/spec.md`; and whether tasks contain at least one non-placeholder checkbox. The vendored validator requires other design fields, including **Security / trust implications**, **Routing / context / memory implications**, **Migration / rollback**, and **ADR impact**, but tests those fields for nonblank body rather than rejection of `TBD` or `N/A`. Do not treat a local admission pass as proof that every mandatory field is substantive.

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

When a run reaches receipt creation, `receipt.json` records the `openspec` block, including the issue ID inferred from the change slug (not fetched from Linear), the full change ID, schema, start digest, and pinned CLI strict-validation result. Early admission/planning failures can return without a receipt.

Receipt verification rebuilds the recorded conformance result from `events.jsonl`. This is an auditable **local** trail for the supplied change and recorded execution, not proof of a Linear lookup, actual external CI/merge, or CERTIFIED release status.

## Archive Status (Operator-Owned)

There is no production `verdict openspec archive` command or runner archive call. `can_archive_openspec_change(change_id, has_merged_main_verification)` is only an advisory boolean helper: it trusts the caller's merged-main flag. It does **not** inspect merged-main evidence, Verdict proof, exact-head CI, or approved review and cannot enforce an archive policy. Operators must verify these external conditions before invoking OpenSpec archive separately. Do not report archive as performed or gated by this runner.

## Out of Scope (Deferred)

- Runner auto-dispatch integration (manual for now)
- Linear issue fetch and status writes (the current binding only infers an issue ID from a change slug)
- Automated evidence-bound archive, exact-head CI and merged-main verification
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

- OpenSpec 1.13.2: the wrapper invokes only `npx -y @fission-ai/openspec@1.13.2` and needs `npx` plus a cached or published package; a global `openspec` installation is not used as a fallback
- verdict-ecosystem@fec5556 (reference schema and validator)
- Python 3.10+

## Orchestration conformance gate

`verdict orchestrate --repo PATH --openspec-change <full-change-slug> "goal"`
binds a run to exactly `PATH/openspec/changes/<full-change-slug>`. The runner
admits on-disk artifacts with vendored checks before work; it does not use the
CLI loader or preflight pinned CLI availability. After execution, and before
its final local receipt verdict, it invokes pinned OpenSpec
`validate <full-change-slug> --strict --json`. The receipt records the CLI
version, command, exit code, JSON-output SHA-256, validity, issues, check time,
and spec digest. A missing/timed-out CLI, invalid JSON/schema, failed strict
validation, or changed revision prevents a local COMPLETE verdict. This is
structural conformance evidence, not independent implementation proof. Verdict's
integration, review, and proof gates still apply. CI, merge, archive, and Linear
Done remain external. Older runs without an OpenSpec block retain their existing
receipt behavior.
