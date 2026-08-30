# Implementation Plan: Live Routing Golden Path

**Branch**: `276-live-routing-golden-path` | **Date**: 2026-08-30 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/276-live-routing-golden-path/spec.md`

## Summary

Verdict must **fetch a live gateway/provider catalog, classify from published specs (never names), select cheaper-first, explain, and execute one named check on a real identity** through that surface. A fixture-only green run is not the feature.

Reuse `verdict-core` catalog, OmniRoute catalog qualification, availability, dispatcher, enforcement, and receipts. Stop name-guessing in `catalog.py`. Add a golden-path orchestrator that talks to a real OpenAI-compatible gateway (default OmniRoute `http://localhost:20128/v1`).

## Technical Context

**Language/Version**: Python 3.12+ as used by `verdict-core`

**Primary Dependencies**: Existing `verdict-core` (uv, pytest). Live OpenAI-compatible gateway (`/v1/models` + chat completions). No new product repo.

**Storage**: In-run catalog snapshot + allowlisted receipts (existing receipt store). No new database.

**Testing**: pytest. Classification/rule tests may use fixtures. Live tests must hit a real gateway; unreachable gateway is blocked, not passed.

**Target Platform**: Linux operator workstation with a local or reachable gateway.

**Project Type**: Library + CLI-style entry in `verdict-core`

**Performance Goals**: One named check; catalog fetch and bounded probe within a single operator run (minutes, not a batch job).

**Constraints**: Fail closed; no secrets in receipts; Core is policy authority; gateway executes only chosen identities; no Hermes keyword routing; no opaque `auto/*`.

**Scale/Scope**: One live gateway, its listed providers/models, one named check, cheaper-first failover across unique qualified identities.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

- Coordination is not execution: live fetch and live checker evidence required; memory/handoffs do not count.
- Documentation before dependencies: OmniRoute catalog/pricing and Hermes provider-routing were read; keyword routing was rejected.
- Repository boundaries: implementation is `verdict-core` only.
- Verification is part of the change: pytest plus a live gateway run; blocked ≠ passed.
- Least authority: receipts allowlisted; no credential persistence.

Post-design: still pass. Live-surface requirement increases verification honesty rather than weakening gates.

## Project Structure

### Documentation (this feature)

```text
specs/276-live-routing-golden-path/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/golden-path.v1.md
└── tasks.md                 # created by /speckit-tasks, not this command
```

### Source Code (`verdict-core`)

```text
verdict/catalog.py                 # stop name heuristics; unclassified if specs missing
verdict/omniroute_catalog.py       # reuse snapshot/freshness
verdict/availability.py            # keep/drop after probe
verdict/dispatcher.py              # cheaper-first among classified identities
verdict/golden_path.py             # NEW orchestrator: fetch, classify, select, execute, receipt
verdict/golden_path_live.py        # NEW live gateway client (/models + completions)
tests/test_golden_path_classify.py # fixtures for rules only
tests/test_golden_path_live.py     # live gateway; blocked if down
```

**Structure Decision**: Extend `verdict-core`. Do not add a service. Isolated git worktree at implement time.

## Complexity Tracking

No constitution violations.

## Phase 0 / Phase 1

See [research.md](./research.md), [data-model.md](./data-model.md), [contracts/golden-path.v1.md](./contracts/golden-path.v1.md), [quickstart.md](./quickstart.md).

Named check: live completion on the selected identity whose body must be exactly JSON `{"golden_path":"ok"}`. Independent checker: parse JSON; pass only if `golden_path == "ok"`. Any other reply fails.

Cost rank: `local` < `free` < `cheaper` < `paid`; ties by `identity_id`.

Live denied class: operator denylist on the live listing.

Pricing: catalog row first; optional same-capture pricing listing; else unclassified.

Docs: `verdict-core/docs/guides/golden-path.md` (T027).

Usage probes (first-party, CodexBar-pattern, not the app):

| Provider | Discover | Fetch |
|----------|----------|--------|
| Codex | `~/.codex/auth.json` or `$CODEX_HOME/auth.json` | `GET https://chatgpt.com/backend-api/wham/usage` with Bearer from that file. Do not write tokens back. |
| Claude | `~/.claude/.credentials.json` | `GET https://api.anthropic.com/api/oauth/usage` (needs usage/profile scope). |
| OpenRouter | `OPENROUTER_API_KEY` / env already used by the gateway | OpenRouter credits API if key present. |
| xAI platform | `XAI_MANAGEMENT_API_KEY` + team id if present | `GET https://management-api.x.ai/v1/billing/teams/{team_id}/prepaid/balance` |

Later phase (US6, P3): opt-in browser-cookie usage probes (Cursor, Claude web extras, Copilot budget extras), CodexBar web strategy. Default off. Not required for the live demo.

Out of scope even later: Keychain writes, PTY scrape of CLIs, mutating auth files.

Timeout-bounded HTTP. Failures skip that provider’s quota signal. Exhausted quota ≠ cheaper.

Add `verdict/golden_path_usage.py` (T010b).
