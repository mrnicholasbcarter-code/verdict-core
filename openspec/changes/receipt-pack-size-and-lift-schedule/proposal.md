# Proposal: Receipt pack size per node + context-lift scheduled bench

## Why

Context packs are wired: `verdict/context_pack.py` has 20 production importers and
`hydrate` events already record `context_bytes`, `prompt_tokens_estimated`, `sources`
and `truncated` per dispatch. Two gaps remain:

1. The run receipt (`verdict/orchestration/receipt.py`) projected each node's routing
   and outcome but discarded all context-pack evidence. Pack size was invisible in
   receipts and in `verdict trace --context`.

2. `context_lift.py` proved lift in ad-hoc runs but had no production entry point, no
   scheduled measurement and no durable evidence.

Closing both gaps advances the headline metric (accepted work per paid dollar) by
making pack evidence observable and by keeping free-model lift data current.

## What Changes

### Part 1 — Pack size per node in the receipt

- `verdict/orchestration/receipt.py`: `_node_record` now handles `hydrate` events.
  When a `hydrate` event is present for the current attempt, its `context_bytes`,
  `prompt_tokens_estimated`, `sources` count and `truncated` flag are projected into
  an optional `context_pack` dict on that attempt row. The field is omitted when
  absent; existing attempts without a hydrate event are unchanged.
- Module docstring updated to document the `hydrate` event convention.
- The events.jsonl digest binding is not changed: `verify_run_receipt` recomputes
  the receipt from events and compares key-by-key; adding a new projection field is
  safe.

### Part 2 — `verdict bench context-lift` and daily systemd timer

- `verdict/commands/parsers_autodev.py`: new `bench` subcommand group with
  `context-lift` sub-subcommand (`--json`, `--base-url`, `--dry-run`,
  `--install-service`, `--uninstall`).
- `verdict/commands/dispatch.py`: routes `bench` → `context-lift`; writes evidence
  to `$VERDICT_HOME/context-lift/{stamp}.json` and `latest.json`; exits 0 on blocked.
- `verdict/context_lift_service.py` (new): idempotent systemd user service + timer
  (`verdict-context-lift.service` / `.timer`). OnCalendar=daily,
  RandomizedDelaySec=10800, Persistent=true. Resource bounds: Nice=19,
  IOSchedulingClass=idle, CPUWeight=10, MemoryMax=256M. Blocked result exits 0.
- `verdict/setup_host.py`: `CONTEXT_LIFT = "host.context-lift-timer"` constant,
  ORDER updated, `_problems_for` case and `run_host_action` case; follows the
  `acceptance_service` / `pr_watcher_service` pattern exactly.
- `verdict/doctor_diagnostics.py`: `_context_lift_timer_row` function appended to
  `installed_service_rows`; reports missing/failed/active state with repair hint.

## Capabilities

### New Capabilities

- `receipt-context-pack`: Per-attempt context-pack evidence in run receipts, projected
  from existing `hydrate` events.
- `context-lift-scheduled`: Daily bench job keeping free-model lift evidence current.

## Impact

**KNOWN**: Changes own `receipt.py`, `context_lift_service.py`, parsers/dispatch
additions and setup/doctor wiring. `runtime.py`, `eligibility.py`, `recovery.py`,
`capacity.py`, `api.py` and `verdict/discovery/` are not touched.

**INFERRED**: Receipts will carry context-pack size for any run whose events include
`hydrate` events. No existing receipt is re-verified; new receipts include the field.
The daily bench produces at most a few free-tier calls per day.

**NOT AVAILABLE**: The timer's first live evidence run requires merged main (post-merge
task below).

## Acceptance Criteria

- [x] `_node_record` projects `context_bytes`, `prompt_tokens_estimated`,
      `sources_count` and `truncated` from `hydrate` events into `context_pack` on the
      correct attempt row; field absent when hydrate is missing.
- [x] `tests/test_orch_receipt.py` passes; digest binding unchanged.
- [x] `verdict bench context-lift --dry-run` exits 0; `--install-service` installs the
      timer units.
- [x] `verdict setup` and `verdict doctor` report the context-lift timer correctly.
- [x] ruff check/format, mypy --strict, reachability ratchet, check_lint_gates.py pass.
- [ ] (post-merge) 1.1 Timer fires and writes evidence under `$VERDICT_HOME/context-lift/`
      on the production host; `verdict doctor` shows context-lift timer ok.
