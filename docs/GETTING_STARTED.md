# Getting Started with Verdict

> **Shipped behavior.** Commands and paths below match the current CLI and config
> loaders. Planned CLI/TUI work
> may extend interactive presentation without changing these entry points.

## Quick Install

> **Which version you get.** 1.0.0 is on PyPI. `pipx`, `pip` and `install.sh`
> all install 1.0.0. See [Install](../README.md#install).

```bash
# Using pipx (recommended)
pipx install verdict-core

# Or with server extras (`verdict serve` needs FastAPI + uvicorn)
pipx install 'verdict-core[server]'

# Or with dashboard extras (Python 3.11+; core Verdict supports 3.10)
pipx install 'verdict-core[dashboard]'

# Or everything `verdict serve` / `verdict ui` need
pipx install 'verdict-core[all]'

# Optional installer (review the script first)
curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh | bash

# Development install with uv
git clone https://github.com/mrnicholasbcarter-code/verdict-core.git
cd verdict-core
uv sync --extra dev --extra server --extra dashboard
```

The optional installer downloads the pinned release, checks its SHA-256 against PyPI, probes
for a local gateway, runs setup, and runs the offline `verdict demo` to show the install works.
It runs `verdict check` once a configuration file exists. `install.sh` is checked in clean
Debian-based Python 3.12 and 3.13 containers. Live provider execution is separate from the
credential-free path.

## First Run

```bash
# Credential-free flagship demo (works from an installed wheel; no API key)
verdict quickstart --json --non-interactive --dry-run

# From a contributor checkout
uv run python -m verdict quickstart --non-interactive --dry-run

# Interactive setup wizard (writes ~/.config/verdict/verdict.yaml)
verdict setup

# Inspect the qualified catalog (named drop reasons)
verdict models

# Forecast a route without a paid call
verdict simulate "Refactor this Python module to use type hints"

# Live route — requires a compatible gateway and admits free∩active only
# An empty intersection fails closed (no silent Opus fallback).
verdict route "Refactor this Python module to use type hints" --terse
```

## Run the Golden Path

The orchestration golden path runs a full orchestration: goal → frontier plan → DAG → eligibility →
parallel workers → recovery → independent review → digest-verified receipt.

```bash
# Full run with chaos flags (faults injected into specific routes)
verdict orchestrate "Add a tested textkit.stats feature"   --inject "cc/claude-sonnet-4-6=quota,no_final"   --inject "cc/*=rate_limit"

# Live TUI view of a running orchestration
verdict watch <run-id>

# Verify the receipt (event-log digest) after completion
verdict run-receipt <run-id>

# Show the eligibility ladder for a route
verdict eligibility
```

See [`docs/guides/orchestration-golden-path.md`](guides/orchestration-golden-path.md) for prerequisites,
a full walkthrough, and the recorded scenario matrix (A–J).

## Configuration

Routing configuration is YAML (not TOML):

1. **Global**: `~/.config/verdict/verdict.yaml` (or `$XDG_CONFIG_HOME/verdict/verdict.yaml`)
2. **Environment variables** override process behaviour (see [CONFIGURATION.md](CONFIGURATION.md))

A narrow `[context]` TOML reader also exists at `.verdict/config.toml` /
`~/.verdict/config.toml` for context-hydration settings only — it is **not** the
routing configuration SoT.

```yaml
# ~/.config/verdict/verdict.yaml
primary_model: anthropic/claude-opus-5   # preferred identity when eligible
providers: {}
```

`primary_model` is a preference string for setup/detection. It is **not** a
fail-open fallback when no candidate survives the eligibility gates.

## Next Steps

- [Orchestration golden path](guides/orchestration-golden-path.md) — End-to-end orchestration walkthrough
- [CLI Reference](CLI_REFERENCE.md) — Commands and flags
- [Configuration](CONFIGURATION.md) — Config paths and environment variables
- [Architecture](architecture.md) — Gate → eligibility → intelligence → dispatch → orchestration
- [User Journey](USER_JOURNEY.md) — Operator walkthrough from install to receipt
- [Evidence Index](proof/EVIDENCE_INDEX.md) — What claims are currently proven
