# User services

## ADDED Requirements

### Requirement: One gateway key resolver
Verdict SHALL resolve the OmniRoute gateway key from the exported environment
first, then the credential store, then the owner-only
`~/.config/verdict/omniroute-api-key` file, then the `omniroute` entry in
Prime's `auth.json`. It SHALL skip a key file that group or others can read.

#### Scenario: Systemd unit without an exported key
- **WHEN** `verdict harness prime sync-models` runs with no gateway key in its environment
- **AND** `~/.config/verdict/omniroute-api-key` exists with mode 0600
- **THEN** the inventory request carries the key from that file

#### Scenario: Exported key wins
- **WHEN** `OMNIROUTE_API_KEY` is exported and a key file also exists
- **THEN** the exported key is used

### Requirement: Units hold no secret
`verdict setup services` SHALL install the prober unit and, when Prime is
installed, the Prime sync unit, each with a timer. Unit files SHALL NOT contain
a key. Install SHALL fail before writing when no key source is readable
without the exported environment.

#### Scenario: Key only in the shell
- **WHEN** the only key is an exported `OMNIROUTE_API_KEY`
- **THEN** `verdict setup services` exits non-zero, names the key file, and writes no unit

#### Scenario: Install is idempotent
- **WHEN** `verdict setup services` runs twice with the same budgets
- **THEN** the second run changes no file and only re-enables the timers

### Requirement: Doctor reports Verdict services
`verdict doctor` SHALL report, for each Verdict unit, whether it is installed,
whether its timer is active, the last result and the last journal line of a
failed run. It SHALL exit non-zero when a unit is missing, inactive or failed,
when the newest health-cache probe is older than twice the prober cycle, or
when the cache has no fresh or stale route. `verdict setup --check` SHALL exit
non-zero when a unit is missing, inactive or failed.

#### Scenario: Stopped prober timer
- **WHEN** `verdict-prove-at-rest.timer` is stopped
- **THEN** `verdict doctor` exits 1 and an issue names `verdict-prove-at-rest.timer`

#### Scenario: Failed sync run
- **WHEN** the last `verdict-prime-sync.service` run exited with status 2
- **THEN** doctor shows the failed unit with its last error line, for example the HTTP 401

#### Scenario: No systemd
- **WHEN** `systemctl` is not installed
- **THEN** doctor reports a warning, not a passing services check

### Requirement: Doctor fails on an empty eligible list with proofs
`verdict doctor` SHALL exit non-zero when the canonical eligibility result has
FREE routes with a current agentic proof but no eligible route.

#### Scenario: Proofs exist, list is empty
- **WHEN** 13 FREE routes hold a current agentic proof and `verdict eligibility` lists 0 eligible
- **THEN** doctor reports "Eligibility list is stale" and exits 1
