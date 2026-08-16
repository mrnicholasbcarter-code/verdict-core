# Getting Started with Verdict

## Quick Install

```bash
# Using pipx (recommended)
pipx install verdict-core

# Or with server extras
pipx install 'verdict-core[server]'

# Or with dashboard extras (Streamlit + Plotly)
pipx install 'verdict-core[server,dashboard]'

# Or universal installer
curl -fsSL https://raw.githubusercontent.com/verdict/verdict-core/main/install.sh | bash

# Development install with uv
git clone https://github.com/verdict/verdict-core.git
cd verdict-core
uv sync --extra dev --extra server --extra dashboard
```

## First Run

```bash
# Credential-free flagship demo (works from installed wheel)
verdict quickstart --json --non-interactive --dry-run

# Or run the source-checkout compatibility wrapper
uv run python scripts/flagship_demo.py

# Interactive setup wizard
verdict setup

# Route a task
verdict route "Refactor this Python module to use type hints" --terse
# → anthropic/claude-3-opus-20240229
```

## Configuration

Verdict uses layered configuration:

1. **Global**: `~/.verdict/config.toml`
2. **Project**: `.verdict/config.toml` (takes precedence)

```toml
# ~/.verdict/config.toml
[gateway]
primary_model = "anthropic/claude-3-opus-20240229"
providers = {}

[intelligence]
profile = "balanced"        # fast | balanced | thorough
timeout_ms = 8000
allow_client_model_override = false

[availability]
ttl_seconds = 60
stale_window_seconds = 30
omniroute_base_url = "http://localhost:20128"  # Optional
```

## Next Steps

- [CLI Reference](CLI_REFERENCE.md) — All commands and flags
- [Configuration](CONFIGURATION.md) — Complete config options
- [Local Development](guides/local-development.md) — Dev environment setup
- [Production Deployment](guides/production-deployment.md) — Deploy to production