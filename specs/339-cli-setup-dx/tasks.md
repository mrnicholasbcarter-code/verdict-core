# Tasks: CLI Setup DX — Spotless End-User Experience

**Feature**: `339-cli-setup-dx` | **Branch**: `feat/339-cli-setup-dx`

**Input**: Design documents from `specs/339-cli-setup-dx/`

**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/ ✓

**Dependencies**: #237 (`[DX-001] verdict init`) must be delivered separately; this feature references but does not implement `verdict init`.

**Tests**: Not requested. No test tasks generated.

## Format: `[ID] [P?] [Story] Description with file path`

- **[P]**: Can run in parallel (different files, no upstream task dependency)
- **[Story]**: User story this task belongs to (US1–US4)
- All file paths are relative to the repo root

---

## Phase 1: Setup (Source Audit — Read-Only)

**Purpose**: Confirm exact source locations and current state of all files that will be changed. Required before any edit to avoid stale-line edits on a 2000+ line file.

- [ ] T001 Read `quickstart.sh` lines 20–50; confirm the exact line that writes `config.yaml` (currently line 28) and the echo on line 45; record both line numbers in a comment at the top of the task for Phase 2 reference. File: `quickstart.sh`
- [ ] T002 [P] Read `verdict/provider_detection.py` lines 80–105 and 360–400; confirm the swapped port label dict entries (9router dict has `default_base_url: http://localhost:20128/v1` and `detect_running` checks 20128; omniroute dict has `default_base_url: http://localhost:20132/v1` and checks 20132 first). File: `verdict/provider_detection.py`
- [ ] T003 [P] Read `verdict/cli.py` lines 606–648 (`cmd_cost_report`) and lines 2700–2720 (`simulate_p` block) to confirm the exact insertion point for the `cost-report` subparser (insert after `simulate_p` block, before `args = parser.parse_args()`). File: `verdict/cli.py`

**Checkpoint**: All line numbers confirmed; Phases 2–6 may proceed.

---

## Phase 2: Foundational (Bug Fixes — Block All Stories)

**Purpose**: Four confirmed bugs from research.md that affect every user story. Must be fixed before story implementation begins.

**⚠️ CRITICAL**: Stories US1–US3 depend on correct port labels and config filename. Fix all four bugs first.

### Bug Fix 1 — quickstart.sh config filename (FR-012)

- [ ] T004 In `quickstart.sh`, change line 28 `"$CONFIG_DIR/config.yaml"` → `"$CONFIG_DIR/verdict.yaml"` and line 45 echo from `config.yaml` → `verdict.yaml`. This aligns quickstart.sh with the filename gate.py reads (`verdict.yaml` at line 73 of gate.py). File: `quickstart.sh`

  **Acceptance criteria**:
  - `grep config.yaml quickstart.sh` returns no output
  - `grep verdict.yaml quickstart.sh` returns both the write line and the echo line

### Bug Fix 2 — provider_detection.py swapped port labels (FR-014)

- [ ] T005 [P] In `verdict/provider_detection.py`, correct the port assignment dict (lines 83–99):
  - `"9router"` entry: `default_base_url` → `http://localhost:20129/v1`; `detect_running` lambda → `_check_port(20129) or _check_port(20132)`
  - `"omniroute"` entry: `default_base_url` → `http://localhost:20128/v1`; `detect_running` lambda → `_check_port(20128) or _check_port(20132)`
  - Also update `_detect_centralized_routers` (lines 366–387): fix omniroute branch to probe 20128 first, 9router branch to probe 20129 first.

  **Acceptance criteria**:
  - `python -c "from verdict.provider_detection import PROVIDER_REGISTRY; r = PROVIDER_REGISTRY['omniroute']; assert '20128' in r['default_base_url'], r['default_base_url']"` exits 0
  - `python -c "from verdict.provider_detection import PROVIDER_REGISTRY; r = PROVIDER_REGISTRY['9router']; assert '20129' in r['default_base_url'], r['default_base_url']"` exits 0

### Bug Fix 3 — unregistered cmd_cost_report (FR-013)

- [ ] T006 [P] In `verdict/cli.py`, add a `cost-report` subparser immediately before `args = parser.parse_args()` (after the `simulate_p` block, ~line 2713):
  ```python
  subparsers.add_parser("cost-report", help="Estimate token cost from routing decision history")
  ```
  Add dispatch in the `main()` elif chain (after the `simulate` branch):
  ```python
  elif args.command == "cost-report":
      cmd_cost_report()
  ```
  File: `verdict/cli.py`

  **Acceptance criteria**:
  - `verdict cost-report --help` exits 0 and prints cost-report help text
  - `verdict --help` lists `cost-report` in the subcommand list

### Bug Fix 4 — OMNIROUTE_BASE_URL not defaulted after detection (Gap #1 from research.md)

- [ ] T007 In `verdict/cli.py`, update the `_omniroute_api_request` fallback at line 49: when `OMNIROUTE_BASE_URL` is unset, probe `http://localhost:20128/api/health` and `http://localhost:20129/api/health`; if either returns `{"status": "ok"}`, use that host as a one-shot base URL for the request (do not write env; only applies within the current process call). This prevents silent `None` returns when OmniRoute is running but the var is unset. File: `verdict/cli.py`

  **Acceptance criteria**:
  - With `OMNIROUTE_BASE_URL` unset and OmniRoute at :20128, `verdict detect --json` does not return `null` for the omniroute base URL
  - With nothing on :20128 or :20129, `_omniroute_api_request` still returns `None` gracefully

**Checkpoint**: All four bugs fixed. US1–US4 story work can begin. T005 and T006 may run in parallel; T004 and T007 are independent of each other.

---

## Phase 3: User Story 1 — Zero-to-Routing in Under Five Minutes (Priority: P1) 🎯 MVP

**Goal**: A new user can copy-paste one command, answer minimal prompts, and run `verdict route` — all under 5 minutes and without reading docs.

**Independent Test** (SC-001, SC-003):
```bash
rm -f ~/.config/verdict/verdict.yaml
bash install.sh          # must complete non-interactively when OmniRoute is at :20128
verdict check            # must exit 0
verdict quickstart       # must exit 0 with a routed result
```

### US1 — GatewayCandidate model (data-model.md §2)

- [ ] T008 [US1] Add `GatewayCandidate` dataclass to `verdict/provider_detection.py` with fields: `host: str`, `port: int`, `url: str`, `health_ok: bool`, `identity: str` (`"omniroute"` / `"9router"` / `"unknown"`), `display_name: str`. Add a module-level function `probe_gateways(ports: list[int] = [20128, 20129, 20132]) -> list[GatewayCandidate]` that executes the three-step detection sequence from `contracts/gateway-health.md`: TCP connect (500ms) → `GET /api/health` (1s) → `GET /api/settings` (1s, optional). File: `verdict/provider_detection.py`

  **Acceptance criteria**:
  - `from verdict.provider_detection import probe_gateways; results = probe_gateways(); assert isinstance(results, list)` exits 0
  - With OmniRoute at :20128, `results[0].identity == "omniroute"` and `results[0].health_ok is True`
  - With nothing on any port, `probe_gateways()` returns `[]`
  - A port with TCP open but failing `/api/health` produces `GatewayCandidate(health_ok=False)` — not counted as valid

### US1 — Wire autodetection into setup wizard (FR-002, FR-003, FR-008)

- [ ] T009 [US1] In `verdict/cli.py` `cmd_setup()`, after the existing `detect_all_providers()` call (~line 114), call `probe_gateways()` from `verdict.provider_detection`. If one or more healthy candidates are found, auto-select the first one and:
  1. Set `config["gateway_url"] = candidate.url` (e.g. `"http://localhost:20128"`)
  2. Print `"OmniRoute detected at {url} — gateway URL written to config."` (no prompt needed per FR-003)
  3. Skip the manual gateway URL prompt for that provider
  If no gateway is found, fall through to existing manual flow (offline-only prompt). File: `verdict/cli.py`

  **Acceptance criteria**:
  - After `verdict setup` (non-interactive mode) with OmniRoute at :20128, `grep gateway_url ~/.config/verdict/verdict.yaml` returns `gateway_url: http://localhost:20128`
  - No `OMNIROUTE_BASE_URL` prompt appears in the wizard output when autodetect succeeds
  - With no gateway, wizard still completes and writes a config (offline_mode: true or empty gateway_url)

### US1 — OMNIROUTE_BASE_URL written to config (FR-008)

- [ ] T010 [US1] In `verdict/cli.py` `cmd_setup()`, when a gateway is autodetected and `config["gateway_url"]` is set, also set `os.environ["OMNIROUTE_BASE_URL"] = candidate.url` for the lifetime of the current process so subsequent calls within setup (e.g. provider node registration) succeed. Do not write `OMNIROUTE_BASE_URL` to the YAML file (it is a runtime env var, not a config field). File: `verdict/cli.py`

  **Acceptance criteria**:
  - Subsequent `_omniroute_api_request` calls within the same `verdict setup` invocation use the detected URL
  - `cat ~/.config/verdict/verdict.yaml` does not contain `OMNIROUTE_BASE_URL` as a YAML key

### US1 — Malformed config edge case (spec edge case §1)

- [ ] T011 [US1] In `verdict/cli.py` `cmd_setup()`, before writing the new config, check whether `config_path` exists and attempt `yaml.safe_load()`; if it raises `yaml.YAMLError`, print `"Warning: existing {config_path} is malformed YAML."` and prompt `"Overwrite it? [Y/n]"`. If user declines, abort setup with exit code 1. File: `verdict/cli.py`

  **Acceptance criteria**:
  - Running `echo 'bad: [unclosed' > ~/.config/verdict/verdict.yaml && verdict setup` prints the warning and prompts for overwrite
  - Answering "n" exits non-zero; answering "y" completes setup normally

### US1 — Install script (FR-001)

- [ ] T012 [P] [US1] Create `install.sh` at repo root. Script must be self-contained and idempotent. Steps:
  1. Detect pip vs pipx: install `verdict-core` via `pipx install verdict-core` if pipx available, else `pip install --user verdict-core`
  2. Detect PATH: if `verdict` binary not found after install, print `"Add ~/.local/bin to your PATH: export PATH=\$PATH:\$HOME/.local/bin"` with the exact fix command
  3. Probe gateways: `curl -sf http://localhost:20128/api/health` and `:20129`; if found, `export OMNIROUTE_BASE_URL=http://localhost:{port}`
  4. Run `verdict setup --non-interactive` if `OMNIROUTE_BASE_URL` is set, else run interactive `verdict setup`
  5. Run `verdict check` and print pass/fail
  6. On success: print `"Verdict is ready. Try: verdict route 'your task here' --criticality medium"`
  Make script executable: `chmod +x install.sh`. File: `install.sh`

  **Acceptance criteria**:
  - `bash install.sh` on a clean machine with OmniRoute at :20128 completes without user input
  - Running `bash install.sh` a second time does not error (idempotent)
  - `shellcheck install.sh` exits 0 (if shellcheck is installed)
  - Script does not use `curl | bash` to execute itself (avoids curl-pipe-bash antipattern)

### US1 — README one-liner reference

- [ ] T013 [US1] In `README.md`, add (or update if a setup section already exists) a "Quick Setup" section at the top with the one-liner: `bash <(curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh)` plus a "From local checkout" variant: `bash install.sh`. File: `README.md`

  **Acceptance criteria**:
  - `grep -A 3 "Quick Setup" README.md` returns the install command
  - The command references `install.sh` at the repo root URL

**Checkpoint**: US1 complete when `verdict check` exits 0 after `bash install.sh` on a machine with OmniRoute running.

---

## Phase 4: User Story 2 — Environment Variable Reference (Priority: P2)

**Goal**: Every supported env var is documented in one place with name, default, and description. `verdict doctor` validates gateway config and reports actionable errors.

**Independent Test** (SC-004, SC-005):
```bash
# Count vars in source vs .env.example
grep -rh 'os\.getenv\|os\.environ\.get' verdict/ | grep -oP '"[A-Z_]{5,}"' | sort -u | wc -l
wc -l .env.example   # must cover all vars from grep output
verdict doctor       # must report config.yaml filename issue if present
```

### US2 — .env.example (FR-009)

- [ ] T014 [P] [US2] Create `.env.example` at repo root. Document all 35+ env vars identified in `research.md §3` and `data-model.md §3`, grouped by purpose with headers. Each entry must have: variable name, example value or `""`, and a `#` comment with type, default, and one-line description. Groups: `# --- Routing ---`, `# --- API Server (verdict serve) ---`, `# --- Memory ---`, `# --- Toolchain / Runtime ---`, `# --- Providers (Cloud — optional) ---`. Warn at the top: `# WARNING: Do not commit this file with real API keys.` File: `.env.example`

  **Acceptance criteria**:
  - `.env.example` contains all vars from research.md §3 table (35+ entries)
  - `grep -c "^[A-Z]" .env.example` ≥ 35
  - Every var has a `#` comment on the line above it
  - File contains the "do not commit" warning at the top
  - `OMNIROUTE_BASE_URL`, `LLMGATE_UPSTREAM_BASE_URL`, `VERDICT_MEMORY_DB`, `XDG_CONFIG_HOME` are all present

### US2 — verdict doctor gateway validation (FR-010)

- [ ] T015 [US2] In `verdict/cli.py` `cmd_doctor()` (~line 1395), add a gateway URL check:
  1. Read `gateway_url` from `~/.config/verdict/verdict.yaml` (if present) and from `os.getenv("OMNIROUTE_BASE_URL")`; prefer env var
  2. If a URL is found, probe `{gateway_url}/api/health` with 2s timeout
  3. If probe fails or returns non-200, append a diagnostic: `"Gateway unreachable at {url}. Run 'verdict detect' to find a running gateway."`
  4. If no URL is configured, append: `"No gateway URL configured. Run 'verdict detect' or set OMNIROUTE_BASE_URL."`
  File: `verdict/cli.py`

  **Acceptance criteria**:
  - With `OMNIROUTE_BASE_URL=http://localhost:1` (unreachable), `verdict doctor` prints a gateway-unreachable message and suggests `verdict detect`
  - With OmniRoute at :20128 and `OMNIROUTE_BASE_URL=http://localhost:20128`, `verdict doctor` reports gateway as reachable
  - With no config and no env var, `verdict doctor` prints the "no gateway URL configured" message

### US2 — verdict doctor config filename check (FR-011)

- [ ] T016 [P] [US2] In `verdict/cli.py` `cmd_doctor()`, add a config filename check:
  1. Compute `config_dir = ${XDG_CONFIG_HOME:-~/.config}/verdict/`
  2. If `config_dir/config.yaml` exists but `config_dir/verdict.yaml` does not, append diagnostic: `"Config file is named 'config.yaml' but must be 'verdict.yaml'. Run: mv {config_dir}/config.yaml {config_dir}/verdict.yaml"`
  3. If `--fix` is passed, perform the rename automatically and print confirmation
  File: `verdict/cli.py`

  **Acceptance criteria**:
  - `echo 'primary_model: x' > ~/.config/verdict/config.yaml && rm -f ~/.config/verdict/verdict.yaml && verdict doctor` prints the rename message
  - `verdict doctor --fix` renames the file and `verdict check` exits 0 afterwards
  - If both files exist, do not rename — warn about duplicate instead

### US2 — verdict doctor env var format validation (US2 AC-3)

- [ ] T017 [P] [US2] In `verdict/cli.py` `cmd_doctor()`, add env var format checks:
  1. If `OMNIROUTE_BASE_URL` is set and does not match `^https?://[^/]+(:[0-9]+)?$`, append: `"OMNIROUTE_BASE_URL has invalid format: '{val}'. Expected http://host:port (no trailing slash)."`
  2. If `OPENAI_API_KEY` is set and does not start with `sk-`, append: `"OPENAI_API_KEY appears invalid (expected prefix 'sk-')."`
  File: `verdict/cli.py`

  **Acceptance criteria**:
  - `OMNIROUTE_BASE_URL=not-a-url verdict doctor` reports the format error
  - `OMNIROUTE_BASE_URL=http://localhost:20128/ verdict doctor` reports trailing-slash error
  - `OMNIROUTE_BASE_URL=http://localhost:20128 verdict doctor` does not report a format error

**Checkpoint**: US2 complete when `verdict doctor` reports all four known config issues and `.env.example` covers all 35+ vars.

---

## Phase 5: User Story 3 — Gateway Autodetection (Priority: P2)

**Goal**: `verdict detect` correctly identifies gateways via HTTP health check (not just TCP), labels them correctly, and provides actionable output for all three scenarios: one found, both found, none found.

**Independent Test** (SC-002):
```bash
verdict detect --json   # with OmniRoute at :20128
# identity field must be "omniroute", health_ok must be true
# port field must be 20128, not confused with 9router
```

### US3 — verdict detect uses HTTP health check (FR-006, FR-007)

- [ ] T018 [US3] Update `cmd_detect()` in `verdict/cli.py` (~line 650) to call `probe_gateways()` (implemented in T008) instead of the existing TCP-only `_detect_centralized_routers()`. Build output from the returned `GatewayCandidate` list. For each candidate, emit: `port`, `url`, `identity`, `health_ok` in `--json` mode and human-readable lines in default mode. File: `verdict/cli.py`

  **Acceptance criteria**:
  - `verdict detect --json` output includes `"identity": "omniroute"` and `"health_ok": true` when OmniRoute is at :20128
  - A port with TCP open but no `/api/health` response is reported as `"health_ok": false` and not listed as an available gateway
  - `verdict detect --json` exit code is 0 even when no gateway is found

### US3 — Both gateways scenario (US3 AC-2)

- [ ] T019 [P] [US3] In `cmd_detect()`, when `probe_gateways()` returns two or more healthy candidates, list all in output and print: `"Multiple gateways found. Set OMNIROUTE_BASE_URL to one of the above to select it."` In `--json` mode, return all candidates in an array. Do not auto-select when multiple are found. File: `verdict/cli.py`

  **Acceptance criteria**:
  - When mocked with two healthy candidates, `verdict detect` lists both and prints the multi-gateway message
  - `verdict detect --json` returns a JSON array with both candidates

### US3 — No gateway found scenario (US3 AC-3)

- [ ] T020 [P] [US3] In `cmd_detect()`, when `probe_gateways()` returns an empty list, print: `"No local gateway found on ports 20128, 20129, 20132."` followed by OmniRoute start instructions: `"To start OmniRoute: npm install -g omniroute && omniroute serve"`. Exit code must still be 0 (detection ran successfully; finding nothing is not an error). File: `verdict/cli.py`

  **Acceptance criteria**:
  - With no services running on 20128/20129/20132, `verdict detect` prints the no-gateway message and exits 0
  - `verdict detect --json` returns `{"gateways": [], "message": "No local gateway found ..."}` (or equivalent empty array)

**Checkpoint**: US3 complete when `verdict detect` correctly identifies OmniRoute (not 9router) at :20128 in 100% of runs where OmniRoute is running, per SC-002.

---

## Phase 6: User Story 4 — Contributor Dev Setup (Priority: P3)

**Goal**: Fresh clone + `uv` → working dev environment with server running and tests available in one command.

**Independent Test** (spec US4 Independent Test):
```bash
bash scripts/dev-start.sh   # must complete on a fresh clone with uv installed
verdict serve --dev          # must start in hot-reload mode
uv run pytest               # must run the test suite
```

### US4 — dev-start.sh (FR-015)

- [ ] T021 [US4] Create `scripts/dev-start.sh`. Steps:
  1. `uv sync --extra dev --extra server --extra dashboard` — sync all dev dependencies
  2. `uv run verdict serve &` — start the server in background (default port)
  3. Print: `"Server started. Run tests with: uv run pytest"`
  4. Print: `"For hot-reload: kill the background server and run: verdict serve --dev"`
  Make executable: `chmod +x scripts/dev-start.sh`. File: `scripts/dev-start.sh`

  **Acceptance criteria**:
  - `bash scripts/dev-start.sh` exits 0 on a fresh clone with `uv` installed
  - After the script runs, `uv run pytest` is available and prints a test count
  - Script does not hardcode absolute paths; uses `uv` from PATH

### US4 — verdict serve --dev flag

- [ ] T022 [US4] In `verdict/cli.py` `cmd_serve()` (or the argparse block for `serve`), add a `--dev` flag that sets `reload=True` on the uvicorn/FastAPI invocation and sets `LLMGATE_AVAILABILITY_PROFILE=development`. Print `"Hot-reload enabled. Edit verdict/ files to auto-restart."` when the flag is used. File: `verdict/cli.py`

  **Acceptance criteria**:
  - `verdict serve --dev --help` lists `--dev` flag
  - Starting with `--dev` prints the hot-reload message

**Checkpoint**: US4 complete when `bash scripts/dev-start.sh` runs end-to-end on a fresh clone with `uv` installed.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T023 [P] Update `verdict/cli.py` `cmd_doctor()` to add a stale config version check: if `~/.config/verdict/verdict.yaml` exists but does not contain a `schema_version` key, print `"Config written by an older Verdict version. Run 'verdict doctor --fix' to migrate."` This satisfies the edge case in spec §edge-cases. File: `verdict/cli.py`
- [ ] T024 [P] Verify that `verdict env` (if it exists as an alias) or the `--help` text for `verdict doctor` references `.env.example`. If no `verdict env` subcommand exists, add a one-line note in `verdict doctor` output pointing to `.env.example` for the full env var reference. File: `verdict/cli.py`
- [ ] T025 Run the quickstart.md validation scenarios end-to-end (Scenario 1 through 5) and confirm all pass. Record any failures as follow-up issues. File: `specs/339-cli-setup-dx/quickstart.md` (reference only — no code changes in this task)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Audit)**: Start immediately; T002 and T003 may run in parallel with T001.
- **Phase 2 (Bug Fixes)**: Starts after Phase 1. T005 and T006 are parallel; T004 and T007 are parallel with each other and with T005/T006.
- **Phase 3 (US1)**: Starts after Phase 2 complete. T008 must precede T009. T009 must precede T010 and T011. T012 and T013 are parallel with T009–T011.
- **Phase 4 (US2)**: Starts after Phase 2 complete. T014, T016, T017 are parallel. T015 depends only on Phase 2.
- **Phase 5 (US3)**: T018 depends on T008 (GatewayCandidate). T019 and T020 are parallel after T018.
- **Phase 6 (US4)**: Independent of Phases 3–5; may start after Phase 2.
- **Phase 7 (Polish)**: After all story phases complete.

### Story Dependencies (parallel-safe once Phase 2 is done)

- **US1** (Phase 3): Depends on T008 (GatewayCandidate from Phase 3 itself).
- **US2** (Phase 4): Independent — no dependency on US1 or US3.
- **US3** (Phase 5): Depends on T008 (GatewayCandidate) — overlaps with US1 start.
- **US4** (Phase 6): Fully independent — can start as soon as Phase 2 is done.

### #237 Dependency

The spec defers `verdict init` to issue #237. Tasks in this feature do NOT implement `verdict init`. If the install wizard prompt for "init" is needed, link to #237 in a TODO comment in `cmd_setup()` rather than implementing.

### Parallel Opportunities

**Phase 2** (all bugs in parallel):
```
T004 (quickstart.sh) || T005 (cost-report parser) || T006 (provider port labels) || T007 (OMNIROUTE_BASE_URL fallback)
```

**Phase 3 + Phase 4 + Phase 6** (after Phase 2):
```
US1 story tasks || US2 story tasks || US4 story tasks
```

---

## Implementation Strategy

### MVP First (US1 only — Stories 2–4 optional)

1. Complete Phase 1: Source audit (T001–T003)
2. Complete Phase 2: All four bug fixes (T004–T007)
3. Complete Phase 3: US1 tasks (T008–T013)
4. **STOP and VALIDATE**: `bash install.sh` → `verdict check` exits 0 in < 5 min
5. Ship MVP; continue to US2–US4 in next increment

### Incremental Delivery

1. Phase 1 + Phase 2 → bugs fixed, baseline stable
2. Phase 3 (US1) → install.sh + gateway wiring → MVP ready
3. Phase 4 (US2) → `.env.example` + `verdict doctor` validation
4. Phase 5 (US3) → `verdict detect` HTTP-validated output
5. Phase 6 (US4) → `scripts/dev-start.sh` contributor setup
6. Phase 7 → Polish and end-to-end validation

---

## Notes

- **No test tasks generated**: Tests not explicitly requested in spec.
- **File size guard**: `verdict/cli.py` is 2930 lines. Keep each edit targeted; do not refactor adjacent code.
- **No breaking changes**: All new subcommands are additive; no existing flags or commands are removed or renamed.
- **Windows out of scope**: `install.sh` and `scripts/dev-start.sh` target bash/zsh on macOS and Linux only.
- **#237 boundary**: This feature references `verdict init` as a dependency but does not implement it. Do not add `verdict init` code here.

---

## Issue Mirror

| Task | Title | Issue URL |
|------|-------|-----------|
| T001 | Audit quickstart.sh — confirm config filename bug lines | https://github.com/mrnicholasbcarter-code/verdict-core/issues/401 |
| T002 | Audit provider_detection.py — confirm swapped port label lines | https://github.com/mrnicholasbcarter-code/verdict-core/issues/402 |
| T003 | Audit cli.py — locate cmd_cost_report and cost-report subparser insertion point | https://github.com/mrnicholasbcarter-code/verdict-core/issues/403 |
| T004 | Fix quickstart.sh — write verdict.yaml not config.yaml (FR-012) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/404 |
| T005 | Fix provider_detection.py — correct swapped OmniRoute/9router port labels (FR-014) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/405 |
| T006 | Register cmd_cost_report as cost-report subcommand in cli.py main() (FR-013) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/406 |
| T007 | Wire OMNIROUTE_BASE_URL autodetect fallback in _omniroute_api_request | https://github.com/mrnicholasbcarter-code/verdict-core/issues/407 |
| T008 | [US1] Add GatewayCandidate dataclass and probe_gateways() to provider_detection.py | https://github.com/mrnicholasbcarter-code/verdict-core/issues/408 |
| T009 | [US1] Wire gateway autodetection into setup wizard (FR-002, FR-003) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/409 |
| T010 | [US1] Write gateway_url to config and set OMNIROUTE_BASE_URL in process (FR-008) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/410 |
| T011 | [US1] Handle malformed verdict.yaml in setup — warn and offer overwrite | https://github.com/mrnicholasbcarter-code/verdict-core/issues/411 |
| T012 | [US1] Create install.sh — idempotent copy-paste setup script (FR-001) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/412 |
| T013 | [US1] Update README.md — add Quick Setup section with one-liner install command | https://github.com/mrnicholasbcarter-code/verdict-core/issues/413 |
| T014 | [US2] Create .env.example — full env var reference (FR-009) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/414 |
| T015 | [US2] Add gateway URL reachability check to verdict doctor (FR-010) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/415 |
| T016 | [US2] Add config filename check to verdict doctor (FR-011) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/416 |
| T017 | [US2] Add env var format validation to verdict doctor | https://github.com/mrnicholasbcarter-code/verdict-core/issues/417 |
| T018 | [US3] Update verdict detect to use HTTP-validated GatewayCandidate list (FR-006, FR-007) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/418 |
| T019 | [US3] Handle multiple gateways in verdict detect output (AC-2) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/419 |
| T020 | [US3] Handle no gateway found in verdict detect (AC-3) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/420 |
| T021 | [US4] Create scripts/dev-start.sh — contributor dev setup (FR-015) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/421 |
| T022 | [US4] Add --dev flag to verdict serve for hot-reload mode | https://github.com/mrnicholasbcarter-code/verdict-core/issues/422 |
| T023 | Add stale config version check to verdict doctor (Polish) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/423 |
| T024 | Add .env.example reference to verdict doctor output (Polish) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/424 |
| T025 | Run quickstart.md validation scenarios end-to-end (Polish) | https://github.com/mrnicholasbcarter-code/verdict-core/issues/425 |
