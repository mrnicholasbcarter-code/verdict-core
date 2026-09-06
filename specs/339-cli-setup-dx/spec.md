# Feature Specification: CLI Setup DX — Spotless End-User Experience

**Feature Branch**: `339-cli-setup-dx`

**Created**: 2026-09-05

**Status**: Draft

**Input**: "the verdict-core CLI must be spotless: research the current CLI setup, provide a copy-paste setup script, local/dev directions, env vars, autodetect gateways, etc. — end users first (not contributors)"

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Zero-to-Routing in Under Five Minutes (Priority: P1)

A new user with Python installed discovers Verdict, copies a one-line install command, pastes it, answers a few prompts, and runs their first routing decision — all without reading documentation or knowing what OmniRoute is.

**Why this priority**: Every friction point before the first successful `verdict route` call is a dropout. If this story fails, every other feature in the CLI is invisible.

**Independent Test**: A fresh machine with Python 3.10+ and no pre-existing Verdict config can run the published setup command, answer prompts, and successfully complete `verdict route "write a blog post" --criticality low` with a routed result — all within five minutes.

**Acceptance Scenarios**:

1. **Given** a machine with Python 3.10+ and pip/pipx installed and no Verdict config, **When** the user runs the published one-command setup script, **Then** Verdict is installed, a valid `~/.config/verdict/verdict.yaml` is written, and `verdict check` exits 0.
2. **Given** OmniRoute is running at localhost:20128, **When** setup runs, **Then** `OMNIROUTE_BASE_URL` is discovered automatically and written into config without the user specifying it.
3. **Given** no gateway is running, **When** setup runs, **Then** the user is prompted to confirm offline-only mode, and setup completes with a credential-free local provider selected.
4. **Given** setup has completed, **When** the user runs `verdict quickstart`, **Then** it exits 0 with a human-readable routing result and no network errors.

---

### User Story 2 — Understanding and Configuring Environment Variables (Priority: P2)

A user who wants to customize Verdict (add a cloud API key, point at a different gateway, change the log path) can look up any environment variable in one place with a description, its default, and an example value — without reading source code.

**Why this priority**: Undocumented env vars force users into source-reading or trial-and-error. This unblocks both power users and integration authors.

**Independent Test**: A user can find, copy, and set any supported env var by reading a single reference file, then verify the change takes effect via `verdict check` or `verdict doctor`.

**Acceptance Scenarios**:

1. **Given** the installed Verdict package, **When** the user runs `verdict env` (or reads the published `.env.example`), **Then** they see every supported environment variable grouped by purpose, with name, type, default, and a one-line description.
2. **Given** a user sets `OMNIROUTE_BASE_URL=http://myhost:20128`, **When** they run `verdict detect`, **Then** Verdict probes that URL and reports connected/offline status.
3. **Given** a user sets an invalid value for a gateway URL, **When** they run `verdict doctor`, **Then** the output names the offending variable, explains what is wrong, and suggests a corrected value.

---

### User Story 3 — Gateway Autodetection (Priority: P2)

When a user has OmniRoute (port 20128) or 9router (port 20129) running locally, Verdict detects the correct gateway automatically and uses it without manual configuration.

**Why this priority**: The most common setup scenario — OmniRoute already running locally — requires zero manual config. Manual URL entry is the single biggest setup friction point today.

**Independent Test**: With OmniRoute running at localhost:20128, run `verdict setup --non-interactive` on a machine with no existing config. The resulting config file must include a correct, reachable gateway URL with no user prompt about the URL.

**Acceptance Scenarios**:

1. **Given** a gateway responds to a health check at localhost:20128, **When** setup or detect runs, **Then** the gateway is selected as the routing target without user input.
2. **Given** gateways at both 20128 and 20129 respond, **When** detect runs, **Then** both are listed and the user is asked to confirm one, with the first-responding one pre-selected.
3. **Given** no gateway responds on any probed port, **When** detect runs, **Then** the output clearly says "no local gateway found" with instructions for starting OmniRoute or using an offline fallback.
4. **Given** a previously-configured gateway URL is unreachable, **When** `verdict doctor` runs, **Then** it reports the gateway as unreachable and offers the `detect` command as a remediation step.

---

### User Story 4 — Contributor Dev Setup (Priority: P3)

A contributor who has cloned the repo can run one command to get a working local development environment with the server running and tests passing, without guessing package manager flags.

**Why this priority**: Contributor DX is secondary to end-user DX per the owner decision, but a broken dev setup creates maintenance drag and discourages contributions.

**Independent Test**: A fresh clone with `uv` installed can reach a running `verdict serve` instance and a passing test suite by following a single `scripts/dev-start.sh` invocation.

**Acceptance Scenarios**:

1. **Given** a fresh clone and `uv` installed, **When** `scripts/dev-start.sh` runs, **Then** dependencies are synced with dev extras, a local server starts on the default port, and the process exits with instructions to run tests.
2. **Given** the dev environment is set up, **When** the user runs `verdict serve --dev`, **Then** the server starts in hot-reload mode with verbose logging and the test suite is available via `uv run pytest`.

---

### Edge Cases

- What happens when the user's `~/.config/verdict/verdict.yaml` exists but is malformed YAML? Setup must warn and offer to overwrite rather than crash.
- What if port 20128 is occupied by a non-Verdict service that does not respond to the health endpoint? Detection must not classify it as a valid gateway; it must only accept responses that match the expected health response shape.
- What if `pip install verdict-core` puts `verdict` on a PATH not in the user's shell? The setup script must detect this and print the PATH fix command.
- What if an API key provided during setup is syntactically invalid (wrong format)? Setup must validate and reject it immediately with a clear message rather than storing it and failing later.
- What if config was written by an old version of Verdict with a different schema? `verdict check` must report the version mismatch and offer a migration command.

---

## Requirements *(mandatory)*

### Functional Requirements

**Setup & Install**

- **FR-001**: A single copy-paste command MUST install Verdict and invoke an interactive setup wizard that completes in fewer than five steps for the common case (local gateway present).
- **FR-002**: The setup wizard MUST probe local ports (at minimum 20128 and 20129) for running gateways before prompting the user for a gateway URL.
- **FR-003**: When a gateway is detected, the wizard MUST populate the gateway URL automatically and not ask the user to type it.
- **FR-004**: The setup wizard MUST write `~/.config/verdict/verdict.yaml` (not `config.yaml`) as the config filename in all code paths (quickstart.sh, `verdict setup`, and any generated snippets).
- **FR-005**: The setup wizard MUST produce a valid config that passes `verdict check` without manual edits.

**Gateway Detection**

- **FR-006**: `verdict detect` MUST confirm a discovered port is a valid, responsive Verdict-compatible gateway (HTTP health check passing) before reporting it as available — TCP connect alone is not sufficient.
- **FR-007**: The detection logic MUST correctly label OmniRoute vs. 9router based on the gateway's own identity response, not port number alone.
- **FR-008**: When a detected gateway URL is auto-populated into config, `OMNIROUTE_BASE_URL` in the config/env MUST be set to the discovered URL.

**Environment Variable Reference**

- **FR-009**: A `.env.example` file MUST exist at the repo root documenting every supported environment variable with name, default value, and a one-line description, grouped by purpose (routing, server, memory, toolchain).
- **FR-010**: `verdict doctor` MUST validate the gateway URL from config/env and report a clear, actionable error if the gateway is unreachable.
- **FR-011**: `verdict doctor` MUST report if the config file uses the wrong filename (`config.yaml` instead of `verdict.yaml`) and offer to rename it.

**Bug Fixes (blocking)**

- **FR-012**: `quickstart.sh` MUST write `verdict.yaml`, not `config.yaml`.
- **FR-013**: `verdict cost-report` MUST be registered as a reachable subcommand in the CLI's argument parser.
- **FR-014**: The port detection labels for OmniRoute and 9router in `provider_detection.py` MUST match the actual default port assignments (OmniRoute: 20128, 9router: 20129).

**Developer Setup**

- **FR-015**: A `scripts/dev-start.sh` script MUST exist that syncs dev dependencies, starts the local server, and prints test-run instructions in a single invocation.

### Key Entities

- **Config file** (`verdict.yaml`): User's primary configuration. Contains gateway URL, primary model, log path. Must be at `${XDG_CONFIG_HOME:-$HOME/.config}/verdict/verdict.yaml`.
- **Gateway**: A running Verdict-compatible routing server (OmniRoute or 9router). Identified by health endpoint response and `runtimePorts` or equivalent identity field.
- **Setup script**: The copy-paste shell snippet published in README and docs. Must be self-contained and idempotent.
- **`.env.example`**: Canonical reference of all supported environment variables. Checked into the repo root.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A new user on a machine with a running local gateway can complete setup and execute `verdict route "test task"` in under 5 minutes with no documentation reading required.
- **SC-002**: `verdict detect` correctly identifies OmniRoute at localhost:20128 and reports its identity (not just "port open") in 100% of cases where OmniRoute is running.
- **SC-003**: `verdict setup` produces a config that passes `verdict check` in a single run, with zero manual edits needed, in the local-gateway scenario.
- **SC-004**: All 35+ environment variables are documented in `.env.example` with no omissions (verified by diffing grep output against the file).
- **SC-005**: `verdict doctor` identifies and reports all four known configuration bugs (wrong config filename, missing gateway URL, unreachable gateway, invalid API key format) with actionable fix instructions.
- **SC-006**: The existing test suite passes without regressions after all bug fixes are applied.

---

## Assumptions

- Users have Python 3.10+ and either `pip` or `pipx` installed before running the setup command. The script does not install Python.
- The feature does not implement `verdict init` (that is issue #237's scope); this spec references #237 as a dependency for the init command.
- OmniRoute's health endpoint at `/api/health` returning `{"status":"ok"}` (or equivalent) is a stable contract. The `runtimePorts` field in `/api/settings` is used for identity confirmation.
- Windows support is out of scope for the setup script in this iteration; the script targets bash/zsh on macOS and Linux.
- Contributor dev setup (Story 4) is secondary to end-user setup (Stories 1–3) and may ship as a follow-up if scope must be cut.
- `LLMGATE_UPSTREAM_BASE_URL` defaults to `http://localhost:20128/v1` in the server; this spec does not change that default — it ensures the CLI-side `OMNIROUTE_BASE_URL` is auto-populated to match.
- The quickstart.sh filename mismatch (FR-012) is treated as a blocking bug, not a config migration. Existing `config.yaml` files written by old quickstart.sh will be flagged by `verdict doctor` (FR-011).

---

## Dependencies

- **#237** `[DX-001] CLI explain, init, local defaults, integration harness` — `verdict init` must be delivered by #237; this spec references it but does not implement it.
- **PR #343 / feat/238-launch-001** — security/privacy gate changes are merged to main before this work starts (avoid rebase conflicts in `verdict/release/`).
