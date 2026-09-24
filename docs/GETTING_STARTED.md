# Getting Started with Verdict

> **Shipped behavior.** Commands and paths below match the current CLI and config
> loaders. Planned CLI/TUI work ([BOD-177](https://linear.app/bodanglin/issue/BOD-177))
> may extend interactive presentation without changing these entry points.

## Quick Install

```bash
# Using pipx (recommended)
pipx install verdict-core

# Or with server extras (`verdict serve` needs FastAPI + uvicorn)
pipx install 'verdict-core[server]'

# Or with dashboard extras (`verdict ui` needs Streamlit + Plotly + pandas)
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

## Configuration

Routing configuration is YAML (not TOML):

1. **Global**: `~/.config/verdict/verdict.yaml` (or `$XDG_CONFIG_HOME/verdict/verdict.yaml`)
2. **Environment variables** override process behaviour (see [CONFIGURATION.md](CONFIGURATION.md))

A narrow `[context]` TOML reader also exists at `.verdict/config.toml` /
`~/.verdict/config.toml` for context-hydration settings only — it is **not** the
routing configuration SoT.

```yaml
# ~/.config/verdict/verdict.yaml
primary_model: anthropic/claude-3-opus-20240229   # preferred identity when eligible
providers: {}
```

`primary_model` is a preference string for setup/detection. It is **not** a
fail-open fallback when no candidate survives the eligibility gates.

## Next Steps

- [CLI Reference](CLI_REFERENCE.md) — Commands and flags
- [Configuration](CONFIGURATION.md) — Config paths and environment variables
- [Architecture](architecture.md) — Gate → eligibility → intelligence → dispatch
- [User Journey](USER_JOURNEY.md) — End-to-end operator walkthrough
- [Local Development](guides/local-development.md) — Dev environment setup
- [Evidence Index](proof/EVIDENCE_INDEX.md) — What claims are currently proven
