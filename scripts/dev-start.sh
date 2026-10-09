#!/usr/bin/env bash
# scripts/dev-start.sh — one-shot contributor dev environment bootstrap.
set -euo pipefail

uv sync --extra dev --extra server --extra dashboard

# Report recommended contributor tooling (non-fatal). Install with: scripts/dev-tooling.sh --install
"$(dirname "$0")/dev-tooling.sh" || true

uv run verdict serve &

echo "Server started. Run tests with: uv run pytest"
echo "For hot-reload: kill the background server and run: verdict serve --dev"
