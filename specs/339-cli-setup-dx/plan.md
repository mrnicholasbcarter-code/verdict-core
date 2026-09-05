# Implementation Plan: CLI Setup DX

**Branch**: `339-cli-setup-dx` | **Date**: 2026-09-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/339-cli-setup-dx/spec.md`

---

## Summary

Deliver a spotless end-user CLI setup experience: a copy-paste install script, gateway autodetection that wires `OMNIROUTE_BASE_URL` automatically, a canonical `.env.example`, four bug fixes (config filename, cost-report registration, port labels, doctor validation), and a `scripts/dev-start.sh` for contributors. Scope is additive — no existing command contracts are broken.

---

## Technical Context

**Language/Version**: Python 3.10+

**Primary Dependencies**: Click/argparse (existing CLI framework), `httpx`/`socket` (port probing — already in use), `uv` (dep management), `yaml` (config read/write — already in use)

**Storage**: `~/.config/verdict/verdict.yaml` (user config), `~/.verdict/memory.db` (memory plane — not modified here)

**Testing**: `pytest`, `uv run pytest`, existing fixture infrastructure in `tests/`

**Target Platform**: Linux / macOS bash/zsh (Windows excluded per spec assumption)

**Project Type**: CLI tool + library

**Performance Goals**: Setup completes in < 5 minutes wall-clock; `verdict detect` completes port probes in < 2 seconds

**Constraints**: Setup script must be idempotent; no new mandatory dependencies; no breaking changes to existing subcommand flags

**Scale/Scope**: Single user machine; affects ~25 env vars, ~6 source files, 1 shell script

---

## Constitution Check

*GATE: Must pass before Phase 0. Re-checked after Phase 1.*

| Principle | Status | Notes |
|---|---|---|
| I. Coordination Is Governance | PASS | No speculative implementation; research.md cites direct source inspection |
| II. Documentation Before Dependencies | PASS | Gateway health endpoint contract verified against live OmniRoute at localhost:20128; no guesses |
| III. Repository Boundaries | PASS | Single repo (verdict-core); no cross-repo changes |
| IV. Verification Is Part of the Change | PASS | Each FR has a measurable SC; test coverage required per tasks |
| V. Safety & Least Authority | PASS | No credential storage; .env.example explicitly warns against committing secrets |

---

## Project Structure

### Documentation (this feature)

```text
specs/339-cli-setup-dx/
├── plan.md              # This file
├── research.md          # Phase 0 — CLI/env/gateway inventory (already written)
├── data-model.md        # Phase 1 — Config schema, gateway identity schema
├── quickstart.md        # Phase 1 — End-to-end validation guide
├── contracts/
│   ├── config-schema.yaml     # verdict.yaml schema
│   └── gateway-health.md      # Health endpoint contract
└── tasks.md             # Phase 2 — /speckit-tasks output (not created here)
```

### Source Code

```text
verdict-core/
├── verdict/
│   ├── cli.py                   # FR-013: register cost-report; FR-002/FR-003: setup wizard gateway detect
│   ├── provider_detection.py    # FR-006/FR-007/FR-014: HTTP health check; fix port labels
│   └── gate.py                  # (read-only — verify config path is verdict.yaml)
├── scripts/
│   └── dev-start.sh             # FR-015: contributor dev setup (NEW)
├── quickstart.sh                # FR-012: fix config filename bug
└── .env.example                 # FR-009: full env var reference (NEW)
```

---

## Phase 0: Research

_All unknowns resolved. See [research.md](research.md) for full findings._

**Summary of resolved decisions**:

| Decision | Chosen | Rationale |
|---|---|---|
| Gateway identity check | `GET /api/health` → `{"status":"ok"}` | Verified against live OmniRoute; TCP connect alone is insufficient |
| OmniRoute identity confirmation | `GET /api/settings` → `runtimePorts.port` | Stable field present in live response; distinguishes OmniRoute from any HTTP service |
| Config filename | `verdict.yaml` | gate.py:73 reads this; quickstart.sh incorrectly uses `config.yaml` |
| OMNIROUTE_BASE_URL default | None (must be set) | `_omniroute_api_request` returns None if unset; `detect` result is not wired to setup — fix: wire autodetect into setup wizard |
| Port assignments (corrected) | OmniRoute: 20128, 9router: 20129 | Verified against running services; provider_detection.py has swapped labels |
| cost-report registration | Missing from argparse `main()` | `cmd_cost_report` is defined at cli.py:605 but has no `add_parser` call |

---

## Phase 1: Design & Contracts

See generated artifacts:

- [data-model.md](data-model.md) — Config schema, gateway identity model, env var taxonomy
- [contracts/config-schema.yaml](contracts/config-schema.yaml) — verdict.yaml schema contract
- [contracts/gateway-health.md](contracts/gateway-health.md) — Health endpoint contract
- [quickstart.md](quickstart.md) — End-to-end validation guide for reviewers and testers

---

## Complexity Tracking

No constitution violations. All changes are additive within a single repository.
