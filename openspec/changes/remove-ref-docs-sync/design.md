# Design: remove the ai-memory reference-docs sync

## Context

`verdict/ref_docs.py` reads the GitHub git tree at the installed OmniRoute tag and writes
one ai-memory page per markdown section. `verdict-ref-docs.timer` runs it hourly.
`verdict doctor` checks both the timer state and the per-project sync state via two rows.
`verdict setup` installs the timer as `host.ref-docs-timer`.

This entire chain depends on ai-memory, which the operator replaced with Hindsight on
2026-11-21 (ADR-050). Removing the chain also removes the only ai-memory dependency
in the `verdict doctor` and `verdict setup` paths.

## Goals / Non-Goals

**Goals:**
- Remove `ref_docs.py`, `ref_docs_service.py` and all callers with no orphan modules.
- Remove the three `memory ref-docs` CLI actions and their parser entries.
- Remove the `_ref_docs_timer_row` and `_ref_docs_rows` doctor functions and their calls.
- Remove the `host.ref-docs-timer` setup provider from `setup_host.py`.
- Leave no broken imports, no stale doc links, no new unreachable code.

**Non-goals:**
- Migrating the ai-memory pages to Hindsight (Hindsight agents use `hindsight_search_pages`).
- Removing the Hermes ai-memory harness or the `AI_MEMORY_AUTH_TOKEN` credential (MEM2).
- Archiving `openspec/changes/ref-docs-in-memory/` (MEM2 completes the decommission).

## Decisions

- Remove as a whole chain: no tombstone module, no deprecation shim.
- `docs/guides/reference-docs.md` becomes a one-line tombstone (inbound links from the
  unarchived `ref-docs-in-memory` change must not break `check_doc_links.py`).
- `tests/test_ref_docs_service.py`, `tests/test_ref_docs.py` and
  `tests/test_ref_docs_integration.py` are deleted entirely; they test only removed code.
- `installed_service_rows` loses its `ref_docs_status` injectable parameter.

## Risks / Trade-offs

- Agents that called `verdict memory ref-docs sync` manually will get a parse error.
  Acceptable: the command is removed in this branch; users are redirected to Hindsight.
- The `openspec/changes/ref-docs-in-memory/` change remains open and unarchived. Its
  `tasks.md` references the ai-memory recall eval (3.2) which is not done. That is
  intentional: MEM2 closes the arc.

## Security / trust implications

The ref-docs sync read public GitHub docs via `gh api` and wrote to the local ai-memory
server. Removing it eliminates one outbound network path and one local write surface.
No trust boundary is weakened.

## Routing / context / memory implications

Agents that relied on ai-memory pages for OmniRoute reference docs should use
`hindsight_search_pages` and `hindsight_read_page` instead. The Hindsight bank `shared`
holds the same content after the operator transfer.

## Migration / rollback

**Migration:** None required. The timer is uninstalled before the new binary is deployed
(`verdict memory ref-docs install-service --uninstall`). `verdict doctor` no longer
checks the timer, so a stopped `verdict-ref-docs.timer` does not cause a doctor failure.

**Rollback:** Revert the branch. The old binary re-registers the timer and doctor row on
next restart.

## ADR impact

ADR-050 authorises this removal. No other ADR is affected.
