# First-run visual system adoption

Home, setup and doctor share `verdict.design` panels, spacing and semantic tokens.
The Verdict wordmark stays static. The previous timed wordmark reveal is removed.
Only observed operations animate: a gateway probe or a bootstrap running event.
The shared activity panel uses `verdict.motion` for a subtle light and border
highlight at five frames per second. No animation sleeps on the input path.
Reduced-motion and plain policies take priority over explicit animation requests.
DEC 2026 groups short frame writes only on supported terminals. Rich restores the
cursor in the task context even when work fails.

## Plain output changes

- Home adds the registered `config.show` action for Configuration.
  Existing commands, gateway facts and receipt facts are preserved.
- A recent run without a receipt now says `NO RECEIPT`, not `RUNNING`: an event-log
  file is not evidence that execution is still active. This removes a guessed
  activity label; it does not change execution state.
- Setup adds a compact stage summary. It records observed bootstrap statuses,
  available plans, and explicit `NOT RUN` for doctor and first-run proof. It never
  claims readiness from install success, and does not reorder bootstrap work.
- Doctor adds repair commands beside problems, without changing diagnosis or
  JSON reports. Capability details and individual findings stay visible.

The `test_visual_system.py` plain hash fixture is intentionally updated only for
home command additions. Setup helper and orchestration bytes in that mixed
fixture remain unchanged. Separate committed home/setup/doctor text goldens
cover 60, 100 and 200 columns. Color tests verify shared palette use, literal
external labels, complete resized borders and accessibility policy.

Context is delivered by the companion context CLI/action lane. Configuration invokes the existing redacted `config.show` action rather
than introducing a parallel implementation.
