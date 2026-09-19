# Prime-style Verdict terminal UX design

Date: 2026-09-19
Status: proposed
Scope: BOD-124 setup/doctor presentation layer and related capability-bootstrap UX

## Intent

Make `verdict setup` and `verdict doctor` feel like a polished product rather than a sequence of shell/Python status lines. Prime Agent's current installer/setup experience is the primary visual and interaction-quality reference, while Verdict keeps distinct branding and its own architecture.

This is not a second installer or bootstrap engine. BOD-124 remains authoritative for discover → normalize → recommend → plan → apply → certify → doctor. The terminal UI renders that real state.

## Evidence from current implementations

Verdict already uses Rich in `verdict/cli.py`, but setup presentation is fragmented across `Panel.fit`, raw `print()`, direct `console.print()`, numbered `Prompt.ask()` menus, and setup-specific strings. `cmd_setup` already delegates bootstrap planning/apply to `verdict.capability_bootstrap`; that separation should be preserved.

Prime Agent's current installer uses a deliberate terminal screen system: semantic color constants, cursor hide/show, synchronized screen updates, TTY detection, terminal-size detection, compact layout fallback, centered content, animation frames, cleanup traps, and plain-output fallback. Its current documented product theme defaults to `prime` on dark terminals and `light` on light terminals. We will borrow those design principles, not its branding or source implementation.

## Approaches considered

### A. Recommended: semantic event/state renderer over BOD-124

Create a small reusable Verdict terminal UI layer. Setup/doctor emit or adapt existing semantic state; renderers present it as interactive Rich TTY, plain text, or JSON.

Advantages:
- preserves BOD-124 authority;
- one visual language across setup and doctor;
- testable without terminal animation;
- supports CI/non-TTY/NO_COLOR/JSON cleanly;
- future harness/gateway/intelligence commands can reuse primitives.

Cost: modest refactor of presentation out of `cli.py`.

### B. Inline beautification inside `cli.py`

Replace current prints with richer panels/spinners in place.

Rejected because it would make an already broad CLI module more presentation-heavy, duplicate state logic, and make JSON/plain/interactive parity harder to prove.

### C. Prime-like full-screen installer separate from Verdict setup

Build a standalone installer UI/state machine.

Rejected because it would duplicate BOD-124 and create two setup authorities.

## Architecture

The core boundary is:

```text
BOD-124 capability bootstrap
 discover → normalize → recommend → plan → apply → certify → doctor
                     │
                     ▼
             semantic UI state/events
                     │
          ┌──────────┼──────────┐
          ▼          ▼          ▼
     Rich/TTY     Plain/CI     JSON
     renderer     renderer     existing
```

No renderer may decide what to install, what is healthy, what is recommended, or whether certification passed.

### Proposed package boundary

Prefer a focused module/package such as `verdict/tui/` if repository conventions support it:

- `theme.py` — semantic Verdict design tokens;
- `models.py` — presentation-only stage/status view models if needed;
- `console.py` — terminal capability and renderer selection;
- `components.py` — header, stage, status tree, plan, warning/error, summary;
- `progress.py` — real-operation spinner/live rendering;
- `setup.py` — map bootstrap report/state into setup presentation;
- `doctor.py` — doctor presentation using the same primitives.

Exact filenames may change during implementation to fit current repo structure. Business logic must not move into this layer.

## Verdict visual language

Prime Agent is the quality benchmark, not a skin to clone.

Verdict should use:

- compact branded header rather than verbose ASCII art;
- strong hierarchy with generous but terminal-efficient whitespace;
- restrained semantic color, with a distinctive Verdict accent;
- thin tree/rail structure for stages and capability groups;
- animated glyph/spinner only while real work is occurring;
- clear `READY`, `PARTIAL`, `BLOCKED`, `FAILED`, `OPTIONAL`, and `RECOMMENDED` semantics;
- a final completion state that summarizes readiness and next action.

Semantic tokens:

- `primary`
- `accent`
- `text`
- `muted`
- `dim`
- `success`
- `warning`
- `error`
- `info`
- `border`

Do not scatter raw RGB/ANSI codes throughout setup logic.

## Interaction design

### Startup

Show a compact Verdict identity and the current operation. Do not clear the terminal unless using an explicitly safe interactive live region. Preserve scrollback wherever practical.

### Discovery

Present real discovered capability groups as a tree:

```text
◆ Inspecting environment
│
├─ ✓ Python 3.13
├─ ✓ uv
└─ ✓ GitHub CLI

◆ Agent harnesses
│
├─ ✓ Codex                 CERTIFIED
├─ ✓ Claude Code           DETECTED
├─ ✓ Hermes                DETECTED
└─ ○ Prime Agent           OPTIONAL
```

A missing optional tool is not an error.

### Recommendations

Recommendations must explain capability value rather than simply listing packages. Example: Serena may be recommended for semantic symbol/refactoring capability; Codebase Memory may be recommended for repository graph/context capability.

### Plan before mutation

Interactive setup shows Install / Configure / Preserve / No change groups before applying mutations. Existing configuration and auth preservation must be explicit.

### Apply/certify

Show the real BOD-124 stages and real operation names. Never invent percentages. Long operations show spinner + elapsed time. If measurable totals exist, bounded counts may be shown; otherwise use indeterminate progress.

### Completion

Three top-level outcomes:

- `VERDICT READY`
- `VERDICT PARTIALLY READY`
- `VERDICT SETUP BLOCKED`

The visual state must match process exit/result semantics.

## Animation behavior

Animations are communication, not decoration.

Use them for:
- active discovery/probing;
- downloads/installation;
- indexing;
- configuration application;
- certification;
- short stage transitions.

Do not animate completed/static output. Do not use fake percentages.

The implementation must:
- restore cursor/state on success, failure, Ctrl-C, SIGTERM where applicable;
- avoid animation in non-TTY output;
- honor `NO_COLOR`;
- honor `TERM=dumb`;
- provide a plain/quiet path;
- leave JSON byte-stable and free of ANSI/progress output;
- degrade on narrow terminals without broken borders/wrapping.

A reduced-animation option is desirable if it fits existing config/CLI patterns, but should not create unnecessary configuration surface.

## Doctor UX

`verdict doctor` shares the same theme/components and becomes the stable post-install health view.

It should group status by semantic subsystem rather than dump probes:

```text
VERDICT DOCTOR

Core                    ✓ healthy
Context Intelligence    ✓ healthy
Capacity Intelligence   ⚠ partial
Proof Pipeline          ✓ healthy

Harnesses
  Codex                 ✓ certified
  Claude Code           ✓ certified
  Hermes                ✓ certified
  Cursor                ⚠ partial route control

Issues
  1 warning
  0 failures
```

Doctor must render actual diagnostic state; it does not invent a parallel health model.

## Bootstrap installer boundary

The public bootstrap remains intentionally small: install Verdict, then direct the user into `verdict setup`. Sophisticated environment discovery, recommendations, mutations, certification, repair, and TUI behavior live in the versioned application layer.

If a shell bootstrap gets Prime-style visual polish, it should remain focused on downloading/verifying/installing Verdict itself and must retain plain/non-TTY fallbacks.

## Error handling

Errors should answer:

1. what failed;
2. what Verdict preserved;
3. whether setup can continue;
4. what the user can run next.

Raw subprocess errors may be included as detail, but are not the primary message.

Partial success is never rendered as full success.

## Testing

Behavior and rendering are tested separately.

Required behavioral cases:
- interactive TTY;
- non-TTY/piped output;
- `NO_COLOR`;
- `TERM=dumb`;
- JSON mode;
- narrow terminal;
- interrupted setup;
- resumed/idempotent setup;
- partial failure;
- successful setup;
- missing optional tool;
- existing healthy install;
- cancellation/Ctrl-C;
- long-running indeterminate operation;
- doctor healthy/partial/failed states.

Golden/snapshot tests may validate stable static frames/components, but must not replace tests of underlying BOD-124 state.

Use controlled terminal dimensions (including ~80 and ~120 columns) for visual regression. Prime Agent's own repository documents tmux-based fixed-dimension TUI testing; Verdict can use the same testing principle without copying its implementation.

## Acceptance criteria

1. `verdict setup` interactive output has a coherent Verdict theme comparable in polish/hierarchy to Prime Agent's current installer.
2. `verdict doctor` uses the same visual system.
3. BOD-124 remains the only setup/bootstrap authority.
4. JSON output is unchanged in semantics and contains no ANSI/live-render artifacts.
5. Non-TTY, `NO_COLOR`, `TERM=dumb`, and narrow-terminal fallbacks are usable.
6. Animations correspond only to real work; no fabricated percentages.
7. Cancellation/failure restores terminal state and reports truthful partial status.
8. Existing user configuration/auth is preserved by the underlying bootstrap contracts.
9. Representative visual output is reviewed at 80/120/wide columns and plain mode.
10. An independent UI review confirms Verdict reaches Prime Agent's class of polish while remaining visibly Verdict, not a clone.

## Non-goals

- copying Prime Agent branding/logo/artwork;
- copying its installer source verbatim;
- introducing another setup state machine;
- making Prime Agent a Verdict dependency;
- changing routing authority;
- hiding failures behind animation;
- turning the installer into a full-screen application when simple live regions/components suffice.

## Implementation sequencing

1. characterize existing setup/doctor outputs and bootstrap result contracts;
2. introduce terminal capability/theme primitives with tests;
3. introduce reusable status/stage/plan/summary components;
4. adapt bootstrap plan/apply output to the renderer without changing bootstrap decisions;
5. migrate classic interactive setup presentation;
6. migrate doctor presentation;
7. add animation/live progress around real long-running operations;
8. add fixed-width visual/golden tests and fallback tests;
9. perform side-by-side visual quality review against current Prime Agent reference;
10. update setup/doctor documentation and screenshots/examples if present.

## Design decision

Proceed with Approach A: a semantic, reusable Verdict TUI renderer layered over BOD-124, with Prime Agent as the primary aesthetic and interaction-quality reference and Verdict-specific branding/state semantics.