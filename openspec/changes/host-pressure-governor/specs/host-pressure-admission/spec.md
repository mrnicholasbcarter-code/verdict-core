# Host pressure admission

## ADDED Requirements

### Requirement: Measured host level

Verdict SHALL derive a GREEN, YELLOW or RED host level from pressure stall
information, MemAvailable, the disk swap I/O rate, ZRAM fill and OOM kills.
ZRAM SHALL NOT count as RAM and swap size SHALL NOT be an input.

#### Scenario: ZRAM-only swapping

- **WHEN** pages are swapped only to a zram device
- **THEN** the disk swap I/O rate is zero and the level does not rise for swapping

#### Scenario: Low MemAvailable with free ZRAM

- **WHEN** MemAvailable is below 10 percent of MemTotal and ZRAM has free space
- **THEN** the level is RED

### Requirement: Hysteresis

A worse level SHALL apply at once. A better level SHALL need three calm samples
at half the thresholds, at least 20 seconds apart, and 120 seconds at the
current level, and SHALL step down one level at a time.

#### Scenario: Recovery after RED

- **WHEN** the host is RED and then calm
- **THEN** the level steps to YELLOW and later to GREEN, never straight to GREEN

### Requirement: Fail-closed evidence

Missing pressure or meminfo evidence SHALL NOT improve the level, and admission
SHALL treat it as full pressure.

#### Scenario: Unreadable PSI

- **WHEN** /proc/pressure/memory cannot be read
- **THEN** the level is at least YELLOW, admission pressure is full, and doctor reports an issue

### Requirement: Admission follows the level

Supervisor admission, orchestrate worker slots, improvement cycles and the
prover's agentic concurrency SHALL use the host level. Running work SHALL NOT
be cancelled when the level rises.

#### Scenario: Orchestrate under pressure

- **WHEN** a run with --max-parallel 4 is running and the level becomes RED
- **THEN** no new worker starts while one is running, running workers finish, and the limit returns to 4 after the level steps down

#### Scenario: Supervisor without evidence

- **WHEN** evaluate_admission is called without pressure evidence
- **THEN** a new story is deferred
