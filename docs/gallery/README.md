# TUI Gallery

Before/after comparison of the Verdict TUI screens across the visual-system redesign (PR #724, commit ba7bdb0).

**Before source:** commit `bf42aed` (last commit before the #724 visual-system merge)
**After source:** regenerated at 110 cols with `scripts/render_tui_gallery.py` from `docs/proof/demo-run`

## Before / After

| Screen | Before (`bf42aed`) | After (`main`) | What changed |
|---|---|---|---|
| Home | [demo.svg](before/demo.svg) | [home-110.svg](after/home-110.svg) | VERDICT pixel-art logo added; charcoal Rich theme; `verdict ›` command prompt (type a goal or a `/` command) |
| Setup | — | [setup-110.svg](after/setup-110.svg) | PRESENTATION FIXTURE label; SETUP / OBSERVED STAGES pipeline (DISCOVER/CERTIFY/PLAN) |
| Doctor | — | [doctor-110.svg](after/doctor-110.svg) | CAPABILITIES grid with HEALTHY/MISSING/DEGRADED status; ISSUES list |
| Cockpit (running) | [demo-tui.svg](before/demo-tui.svg) | [cockpit-running-110.svg](after/cockpit-running-110.svg) | RUNNING header with live node count; GOAL/UNDERSTAND/SELECT/PLAN panels at 110 cols |
| Cockpit (failure) | [demo-tui.svg](before/demo-tui.svg) | [cockpit-failure-110.svg](after/cockpit-failure-110.svg) | ✗ TERMINAL_FAILURE row; FAILURE/REASSIGN panel with REROUTE annotation |
| Cockpit (complete) | [demo-tui.svg](before/demo-tui.svg) | [cockpit-complete-110.svg](after/cockpit-complete-110.svg) | COMPLETE header; all 3 nodes VALIDATED; full failure/reassign history |
| Routing explorer | — | [routing-explorer-110.svg](after/routing-explorer-110.svg) | Recorded routing evaluation table; MISMATCH row |
| Context view | — | [context-view-110.svg](after/context-view-110.svg) | Context budget panel with per-run lines |
| Trace | — | [trace-110.svg](after/trace-110.svg) | 50-step TIMELINE view |
| Demo claims | — | [demo-claims-110.svg](after/demo-claims-110.svg) | CLAIMS VERIFIED table with evidence column |

— = screen did not exist before the redesign.
