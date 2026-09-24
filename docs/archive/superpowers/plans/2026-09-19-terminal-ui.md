# Verdict terminal UI implementation

Goal: a reusable presentation layer for real setup and doctor state, preserving
BOD-124 consent, ownership, certification, and JSON contracts.

## Prime comparative review

Reviewed upstream PrimeIntellect-ai/prime-agent at
`63d88319bf5870cf609fef01f9042bbf431d7df2` (2026-09-19), including `install.sh`,
`prime.json`, onboarding choice/picker/splash components. Executed the upstream
two-frame `scripts/preview-installer-splash.sh 2` in a PTY without installing.

- Palette: named semantic roles, near-neutral surfaces, restrained purple primary,
  muted metadata, separate success/warning/error colors. Installer colors differ
  somewhat from the interactive theme; coherence comes from hierarchy.
- Hierarchy/spacing: stable header, one primary operation, secondary explanation,
  generous whitespace. Choices share a leading inset and highlighted caret row.
- Borders: restrained separators and low-contrast boundaries; not boxes everywhere.
- Animation: installer pulses while the child process runs (180ms); onboarding
  decorative field refreshes at 120ms. Verdict adopts real-work feedback only.
- Prompts/selectors: arrow/confirm/cancel bindings, muted identifiers, searchable
  six-row picker, pinned Continue, connected state, descriptive footnotes.
- Responsive: installer re-reads terminal size, artwork needs 42x22, compact mode
  below 17 rows / 32 safe columns. Content is capped, wrapped/truncated by cell
  width. Secondary detail yields before primary labels.
- Redraw: synchronized frames, home cursor, clear on resize, hide cursor while
  drawing; EXIT/INT/TERM/HUP cleanup restores it. Interactive timer is disposed.
- Outcomes: real command exit status controls failures, captured failure output is
  surfaced. Plain installer bypasses animation on redirected stdout, TERM=dumb,
  or PRIME_AGENT_INSTALLER_PLAIN. Its screen initializer does not check NO_COLOR;
  Verdict explicitly will.

Reference: https://github.com/PrimeIntellect-ai/prime-agent/tree/63d88319bf5870cf609fef01f9042bbf431d7df2

## Design and implementation plan

Verdict uses teal primary, blue secondary, amber accent, terminal-default text,
muted borders, explicit status words and symbols. An inline wordmark and bounded
panels preserve scrollback. No Prime artwork, branding, or source is reused.

Architecture: bootstrap emits optional semantic events, the presenter consumes
events and existing reports, Rich owns rendering and cursor lifecycle. A plan
confirmation callback feeds the existing consent gate; no new installer runner.
JSON bypasses presentation. Plain output retains explanations without controls.
Classic setup remains supported and uses the shared header/prompts/feedback.

- [ ] Add failing terminal invariant/report tests and bootstrap event-order tests.
- [ ] Implement terminal tokens, header, statuses, panels, capability/recommendation
  views, plans, elapsed spinner, safe confirmation, doctor and summaries.
- [ ] Add optional bootstrap progress and pre-apply confirmation seams; preserve
  the canonical algorithm and ownership behavior, including resumed actions.
- [ ] Integrate setup, setup-plan, classic wizard, doctor; preserve JSON envelopes.
- [ ] Verify fallback matrix, widths 40/80/120/180, failure/cancellation and real CLI
  transcripts. Record output examples and limitations.
- [ ] Run full pytest, Ruff format/lint, strict mypy, proof/schema checks, package
  smoke; commit coherently, update safely, push PR, repair CI and review final diff.

Review focus: untrusted labels with terminal controls/markup; partial install
results despite an ok stage; no completed-action replay; no prompt in machine or
redirected input; cursor restored when a real operation raises BaseException.
