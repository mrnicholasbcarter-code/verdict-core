#!/usr/bin/env bash
# scripts/dev-start.sh — one-shot contributor dev environment bootstrap.
set -euo pipefail

uv sync --extra dev --extra server --extra dashboard

uv run verdict serve &

echo "Server started. Run tests with: uv run pytest"
echo "For hot-reload: kill the background server and run: verdict serve --dev"
