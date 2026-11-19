## ADDED Requirements

### Requirement: Serve refuses to start with one actionable line
`verdict serve` SHALL refuse to start when `LLMGATE_AUTH_TOKEN` is unset (and anonymous loopback mode is not enabled) or when authenticated mode has no `VERDICT_RECEIPTS_DB`. It SHALL print one line naming the missing setting and the command or documentation section that fixes it, and exit 2, with no traceback and no assumed default receipts path.

#### Scenario: Fresh install without a token
- **WHEN** `verdict serve` runs with no `LLMGATE_AUTH_TOKEN`
- **THEN** it prints one line naming `LLMGATE_AUTH_TOKEN` and `verdict setup --apply --allow host.serve`, exits 2 and never starts the server

#### Scenario: Token without a receipts path
- **WHEN** `LLMGATE_AUTH_TOKEN` is set and `VERDICT_RECEIPTS_DB` is not
- **THEN** it prints one line naming `VERDICT_RECEIPTS_DB`, exits 2 and never starts the server

### Requirement: Doctor separates not-configured from failed before setup
Before `verdict setup` has written `verdict.yaml`, `verdict doctor` SHALL mark a row whose part does not exist yet as `not-configured`, keep its next command, print "Verdict is installed but not set up yet. Start with: verdict setup" first, and keep exiting non-zero. A `not-configured` row SHALL never be reported as ok. A part that exists but is broken, and every row after setup, SHALL keep its own status.

#### Scenario: Fresh install
- **WHEN** no configuration exists and a timer unit is missing
- **THEN** the row is `NOT-CONFIGURED` with its repair command and doctor exits 1

#### Scenario: Broken part before setup
- **WHEN** no configuration exists and a unit exists but failed, or the health cache is unreadable
- **THEN** the row stays `FAILED`

### Requirement: Missing agentic proof is explained without changing admission
When orchestration selects no route and candidates were rejected for missing agentic proof (`no_agentic_probe` or `no_health_cache`), the BLOCKED reason SHALL say so in plain words and name the bounded command `verdict identify --screen --allow-live-probe --capacity free --max-per-provider 1 --max-routes 8`. The explanation SHALL NOT admit, retry or reorder any route.

#### Scenario: Cold state directory
- **WHEN** every candidate was rejected with `no_agentic_probe`
- **THEN** the run ends BLOCKED and its reason contains the screen command, with no route selected
