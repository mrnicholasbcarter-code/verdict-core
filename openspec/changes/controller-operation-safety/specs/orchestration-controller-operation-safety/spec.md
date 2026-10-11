# Controller operation safety

## Purpose
Keep supervised controllers alive during known bounded planning operations while
preserving real stall detection and preventing unsafe detached-worker replacement.

## ADDED Requirements

### Requirement: Bounded waiting is distinct from progress
The supervisor SHALL permit a valid owner-bound initial or repair planning wait
until its fixed operation deadline, capped at 600 seconds and the overall run
deadline. Waiting SHALL NOT be reported as progress or model health. Invalid,
stale, extended, reopened or unowned leases MUST NOT suppress stall detection.

#### Scenario: Quiet bounded initial planner
- **WHEN** a controller waits quietly on a registered 600-second planner operation
  and ordinary progress has stopped for 240 seconds
- **THEN** supervision reports bounded waiting and does not preempt the operation
  before its deadline

#### Scenario: Expired or forged operation
- **WHEN** a valid operation deadline expires, or a record is malformed or extended
- **THEN** an expired operation terminates supervision, and invalid records retain
  ordinary stall enforcement without granting additional time

#### Scenario: No owned operation
- **WHEN** progress stalls with no valid active operation
- **THEN** the ordinary stall watchdog remains effective

### Requirement: Overall deadline is authoritative
The supervisor MUST enforce its absolute run deadline before accepting completion,
controller exit or new generation dispatch. Cancellation MUST revoke the current
generation even during an active operation or spawn handshake.

#### Scenario: Late completion
- **WHEN** a completion event is visible at or after the absolute run deadline
- **THEN** the supervisor fails closed instead of accepting COMPLETE

### Requirement: Ownership-safe detached worker cleanup
Supervised Prime payloads MUST register durable run/generation/session identity
before execution and join an explicitly configured delegated cgroup-v2 generation
leaf. Missing or unsupported delegation MUST block payload execution. Kernel containment
absence MUST be verified before replacement; process enumeration alone is not proof. Revocation MUST prevent subsequent payload execution. Controller
replacement MUST wait for verified absence of executing generation-contained processes.
Unknown ownership or unresolved launch intent MUST block replacement. Stale or
reused PIDs MUST NOT authorize signals to unrelated processes.

#### Scenario: Controller dies while detached worker runs
- **WHEN** a controller dies with a registered detached Prime session executing
- **THEN** supervised cleanup terminates its contained processes (including normal fork/setsid descendants) before
  replacement starts

#### Scenario: Death at registration boundary
- **WHEN** a launch has durable pending intent but registration cannot be verified
- **THEN** replacement is blocked without claiming that all workers are absent

#### Scenario: Stale worker identity
- **WHEN** a recorded PID now has a different process start identity
- **THEN** no signal is sent to that unrelated process and cleanup fails closed
