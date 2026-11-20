# Design: Receipt pack size per node + context-lift scheduled bench

## Context

`verdict/orchestration/receipt.py` builds per-node summaries from events.jsonl.
`runtime.py` already emits `hydrate` events with `context_bytes`,
`prompt_tokens_estimated`, `sources` and `truncated` per dispatch, but `_node_record`
discarded those events. `context_lift.py` proved lift in ad-hoc runs but lacked a
production entry point, CLI and scheduled measurement.

## Goals / Non-Goals

**Goals**: project context-pack evidence from existing `hydrate` events into per-attempt
receipt rows; add `verdict bench context-lift` CLI; install a daily systemd timer via
`verdict setup`; expose the timer state in `verdict doctor`.

**Non-goals**: change events.jsonl schema; modify the digest binding; run live model
calls in tests; change `runtime.py`, `eligibility.py`, `recovery.py`, `capacity.py`,
`api.py` or `verdict/discovery/`.

## Decisions

1. Project `context_pack` as an optional dict on the attempt row (omit when no hydrate
   event). This follows the `no_change_nodes` omit-when-empty pattern and avoids null
   pollution in existing receipt consumers.
2. `context_lift_service.py` follows `acceptance_service.py` / `pr_watcher_service.py`
   exactly: `SERVICE`, `TIMER`, `timer_state()`, `manage_service()`. The timer uses
   `OnCalendar=daily`, `RandomizedDelaySec=10800` (3 h jitter), `Persistent=true`.
3. The service exits 0 on blocked (gateway down / no free identity). Doctor only fails
   when the unit itself is broken, not when no free route is available.
4. Resource bounds: `Nice=19`, `IOSchedulingClass=idle`, `CPUWeight=10`,
   `MemoryMax=256M` — lowest-priority host pressure.
5. The `bench context-lift` subcommand writes evidence to
   `$VERDICT_HOME/context-lift/{stamp}.json` and `latest.json`. An explicit
   `--install-service` flag handles the service install/uninstall inline.

## Risks / Trade-offs

Existing committed receipts in `docs/proof/` bundles have no `context_pack` field;
`verify_run_receipt` is key-by-key (not hash-of-receipt), so adding a new field to the
projection is safe and does not invalidate old bundles.

The daily timer makes at most a few free-tier model calls per day. A host without a
free identity silently writes a blocked result and exits 0.

## Security / trust implications

No new credentials are introduced. The service runs as a user unit under the operator's
account. `context_bytes`, `prompt_tokens_estimated`, `sources_count` and `truncated`
are integer/boolean/count projections of existing event data — they carry no secrets.
Evidence files are written under `$VERDICT_HOME/context-lift/`, operator-owned and not
committed to the repository.

## Routing / context / memory implications

No changes to candidate admission, ranking or route membership. The timer dispatches
only to free-capacity routes via the existing `run_context_lift` filter. No durable
memory writes occur in this change.

## Migration / rollback

No migration needed. Old receipts without `context_pack` remain valid. Removing the
timer requires `verdict bench context-lift --install-service --uninstall` or
`systemctl --user disable --now verdict-context-lift.timer`. No data loss on removal.

## ADR impact

No new ADR needed. The change follows ADR-036 (goal-to-receipt) receipt projection
conventions and ADR-012 (consented-budgeted probes) for the daily bench timer.
