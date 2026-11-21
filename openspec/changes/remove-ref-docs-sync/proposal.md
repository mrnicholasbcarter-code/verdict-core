# Proposal: remove the ai-memory reference-docs sync (MEM1)

## Why

POSITIVE. The operator replaced ai-memory with Hindsight on 2026-11-21 (ADR-050). Verdict
still synced upstream docs into ai-memory every hour via `verdict-ref-docs.timer`, and
`verdict doctor` failed when that sync lagged. That sync caused the 15:00Z acceptance run
to go red after the OmniRoute 3.8.54 upgrade. A red doctor blocks the Gate 1 count.

## What Changes

- Deleted: `verdict/ref_docs.py`, `verdict/ref_docs_service.py` and their tests.
- Removed CLI subcommand: `verdict memory ref-docs sync|status|install-service`.
- Removed doctor rows: the reference-docs timer row and per-project sync rows.
- Removed setup provider: `host.ref-docs-timer` is no longer planned or applied by
  `verdict setup`.
- Updated: `AGENTS.md` "Memory first" rule names Hindsight; "Legacy names" stops listing
  Hindsight as removed. `ETHOS.md` Decisions and NOW reflect the operator's choice.
- New: ADR-050, decision D33, this change.

## Capabilities

### Modified Capabilities

The `setup-host-prerequisites` capability is modified: the `host.ref-docs-timer`
prerequisite and its associated doctor rows are removed. `verdict doctor` no longer
checks the reference-docs sync timer.

## Impact

- Deleted: `verdict/ref_docs.py`, `verdict/ref_docs_service.py`,
  `tests/test_ref_docs.py`, `tests/test_ref_docs_integration.py`,
  `tests/test_ref_docs_service.py`, `tests/fixtures/ref_docs/`.
- Changed: `verdict/doctor_diagnostics.py`, `verdict/setup_host.py`,
  `verdict/actions/extra.py`, `verdict/actions/registry.py`,
  `verdict/commands/parsers_runtime.py`, `verdict/cli.py`, test fixtures and helpers.
- No new dependency added or removed.

## Evidence boundary

**Merged** — MEM1 (#1253) is squash-merged to main, CI green on exact head SHA. `verdict
doctor` is green with `ai-memory.service` stopped (post-merge acceptance run). The ref-docs
timer is absent from `verdict setup --plan` output.
