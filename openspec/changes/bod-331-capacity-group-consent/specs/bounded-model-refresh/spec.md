# Spec Delta: Per-capacity-class manual refresh consent

## MODIFIED Requirements

### Requirement: TUI manual refresh and probe SHALL use default-No consent

`/eligibility refresh` and `verdict eligibility --verified --refresh` SHALL show
the exact plan and its capacity groups (FREE, SUBSCRIPTION, UNKNOWN, METERED),
each with its routes and estimated requests. UNKNOWN and METERED groups SHALL
also show an estimated spend, or state that it is unknown. The first prompt
SHALL default to No and offer per-group confirmation or "free only". Group
prompts SHALL default to yes for FREE and SUBSCRIPTION and to no for UNKNOWN
and METERED. Blank/n at the first prompt, or EOF, Esc or Ctrl-C at any prompt,
SHALL approve nothing and make no live transport calls. Execution SHALL probe
only the approved groups of the digest-bound plan.

Without a terminal, no prompt SHALL be shown. FREE and SUBSCRIPTION groups SHALL
run within caps. UNKNOWN routes SHALL run only with `--include-unknown`, and
METERED routes only with `--allow-metered`.

#### Scenario: Free only never probes costly routes
- **WHEN** a plan has FREE, SUBSCRIPTION, UNKNOWN and METERED routes and the operator chooses free only
- **THEN** only FREE routes are probed, and the others report `not_approved` with zero calls

#### Scenario: Run without a terminal skips costly groups by default
- **WHEN** `verdict eligibility --verified --refresh` runs without a terminal and without opt-in flags
- **THEN** UNKNOWN and METERED routes get zero calls, and the output names the flag that would include them

#### Scenario: Default-No manual refresh sends no requests
- **WHEN** the operator answers blank/n at the first prompt, ends input, or cancels at any prompt
- **THEN** the action makes no live transport calls and retains last-known evidence
