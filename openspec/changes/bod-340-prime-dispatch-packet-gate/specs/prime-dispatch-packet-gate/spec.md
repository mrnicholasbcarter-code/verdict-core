## ADDED Requirements

### Requirement: Prime dispatch validates a current packet before any worker starts
The Prime runtime bridge MUST call `validate_dispatch` from the controller checkout's
`scripts/prime_workflow.py` before it creates a worker-run directory or launches
`verdict.worker_runtime`. A refusal MUST raise and MUST NOT launch a process.

#### Scenario: Current packet is dispatched with its identity bound
- **WHEN** the packet validates, its worktree is the dispatched repo on the packet branch,
  `base_sha` is an ancestor of HEAD, the supplied Linear `updatedAt` equals the packet's,
  and the issue lease is active, unstale and matches the packet attempt and generation
- **THEN** exactly one worker launch happens and `config.json` records the packet path,
  `attempt_id` and `lease_generation`
- **TEST** `tests/test_prime_dispatch_gate.py::test_bridge_launches_current_packet_and_binds_its_identity`,
  `::test_current_packet_is_admitted_with_its_lease`, `::test_worker_commits_on_top_of_base_stay_current`

#### Scenario: Stale or invalid packet is refused
- **WHEN** the packet is invalid, names another worktree, the Linear revision changed, the
  branch differs, `base_sha` is not an ancestor of HEAD, or the lease is missing, stale or
  of another generation
- **THEN** dispatch raises `ValueError` naming the reason and no worker is launched
- **TEST** `tests/test_prime_dispatch_gate.py::test_invalid_or_stale_packet_is_refused`,
  `::test_bridge_refuses_stale_packet_before_any_worker_launch`

### Requirement: Every worker launch site is covered by the gate
Every production file under `verdict/`, `scripts/` or `.prime/` that launches
`python -m verdict.worker_runtime` MUST call `validate_dispatch` before the launch.

#### Scenario: A new launch site without the gate
- **WHEN** a production file launches `verdict.worker_runtime` without calling
  `validate_dispatch` first
- **THEN** the caller-coverage test fails
- **TEST** `tests/test_prime_dispatch_gate.py::test_every_worker_runtime_launch_site_calls_validate_dispatch_first`
