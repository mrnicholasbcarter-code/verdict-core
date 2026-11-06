# Proposal: upstream reference docs in ai-memory, synced to the installed version

## Why

Workers crawled the OmniRoute docs by hand at real token cost, and two died mid-crawl.
Every session and worker should recall the docs of the tools Verdict depends on cheaply,
current to the version that actually runs, without reading large HTML pages.

## What Changes

- `verdict/ref_docs.py`: reads the GitHub git tree at the installed tool's tag through
  `gh api` and writes one cited page per markdown section (at most 3 KB) into the tool's own
  ai-memory project (`ref-omniroute`). An OpenAPI file becomes one page group per tag.
  A manifest under `$VERDICT_HOME/ref-docs/` makes the sync incremental by blob sha.
- `verdict memory ref-docs sync|status|install-service`.
- `verdict-ref-docs.timer`: hourly, installed by `verdict setup` as host prerequisite
  `host.ref-docs-timer`, resource-bounded with systemd controls.
- `verdict doctor`: one row for the timer and one row per reference project (tag, installed
  tag, last sync, pages).
- `scripts/memory_recall_eval.py`: the operator golden recall eval, scoped to the requested
  project, which also counts top-5 hits from any other project.

## Capabilities

### New Capabilities

- `upstream-reference-docs`: cited, read-only upstream reference pages in ai-memory that follow
  the installed version and never enter operator-rule scopes.

## Impact

- New: `verdict/ref_docs.py`, `verdict/ref_docs_service.py`, `scripts/memory_recall_eval.py`,
  their tests, `tests/fixtures/ref_docs/`.
- Changed: `verdict/setup_host.py`, `verdict/doctor_diagnostics.py`, `verdict/cli.py`,
  `verdict/commands/parsers_runtime.py`, `docs/guides/reference-docs.md`.
- No new dependency. Writes only to the local ai-memory server and `$VERDICT_HOME/ref-docs/`.

## Evidence boundary

KNOWN: OmniRoute v3.8.53 is installed (`omniroute.service` runs that package); the slice-1 sync
wrote 1436 pages and a re-run wrote 0. KNOWN: the original 40-question operator golden set was
lost in the 2026-11-04 host move; it is rebuilt outside the public repository because it quotes
private operator rules. NOT AVAILABLE: a host-pressure governor in Verdict code; the timer uses
systemd resource controls instead.
