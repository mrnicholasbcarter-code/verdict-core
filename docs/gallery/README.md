# TUI Gallery

Before/after comparison of the Verdict TUI screens across the visual-system redesign (PR #724, commit ba7bdb0).

**Before source:** commit `bf42aed` (last commit before the #724 visual-system merge)
**After source:** regenerated at 110 cols with `scripts/render_tui_gallery.py` from `docs/proof/demo-run`

## Before / After

| Screen | Before (`bf42aed`) | After (this branch) | What changed |
|---|---|---|---|
| Home | [demo.svg](before/demo.svg) | [home-110.svg](after/home-110.svg) | VERDICT pixel-art logo added; dark Textual theme; structured panel layout |
| Setup | — | [setup-110.svg](after/setup-110.svg) | New PRESENTATION FIXTURE label; staged pipeline view; COVERED/MISSING markers |
| Doctor | — | [doctor-110.svg](after/doctor-110.svg) | HEALTHY/MISSING/DEGRADED capability grid; ISSUES list; colour-coded severity |
| Cockpit (running) | [demo-tui.svg](before/demo-tui.svg) | [cockpit-running-110.svg](after/cockpit-running-110.svg) | Live node bullets; SELECT/PLAN state; wide column layout at 110 cols |
| Cockpit (failure) | [demo-tui.svg](before/demo-tui.svg) | [cockpit-failure-110.svg](after/cockpit-failure-110.svg) | TERMINAL_FAILURE row in red; QUOTA/COOLDOWN expiry; REROUTE annotation |
| Cockpit (complete) | [demo-tui.svg](before/demo-tui.svg) | [cockpit-complete-110.svg](after/cockpit-complete-110.svg) | COMPLETE header in green; all nodes VALIDATED; full failure/reassign history |
| Routing explorer | — | [routing-explorer-110.svg](after/routing-explorer-110.svg) | Recorded evaluation table; MISMATCH row; selection rationale |
| Context view | — | [context-view-110.svg](after/context-view-110.svg) | Budget bar (KB/limit); per-node context line counts |
| Trace | — | [trace-110.svg](after/trace-110.svg) | 50-step TIMELINE; failure=red, reassign=purple, verify=green |
| Demo claims | — | [demo-claims-110.svg](after/demo-claims-110.svg) | VERIFIED evidence refs; NOT SHOWN annotations |

— = screen did not exist before the redesign.
