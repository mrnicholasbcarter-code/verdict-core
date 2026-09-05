#!/usr/bin/env bash
# verdict-core/install.sh — self-contained, idempotent setup script.
#
# Quick start:
#   bash <(curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh)
# From a local checkout:
#   bash install.sh

set -euo pipefail

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# 1. Install verdict-core via pipx if available, else pip --user.
if command -v verdict >/dev/null 2>&1; then
  log_info "verdict is already installed ($(command -v verdict)); skipping install step."
elif command -v pipx >/dev/null 2>&1; then
  log_info "Installing verdict-core via pipx..."
  pipx install verdict-core
else
  log_info "pipx not found; installing verdict-core via pip --user..."
  pip install --user verdict-core
fi

# 2. Detect PATH: warn if the verdict binary still cannot be found.
if ! command -v verdict >/dev/null 2>&1; then
  log_warn "The 'verdict' command was not found on your PATH."
  echo "Add ~/.local/bin to your PATH: export PATH=\$PATH:\$HOME/.local/bin"
  export PATH="$PATH:$HOME/.local/bin"
fi

if ! command -v verdict >/dev/null 2>&1; then
  log_error "verdict is still not on PATH after adding ~/.local/bin. Aborting."
  exit 1
fi

# 3. Probe known local gateway ports for a running OmniRoute/9router instance.
GATEWAY_URL=""
for port in 20128 20129; do
  if curl -sf "http://localhost:${port}/api/health" >/dev/null 2>&1; then
    GATEWAY_URL="http://localhost:${port}"
    log_success "Detected a local gateway at ${GATEWAY_URL}"
    break
  fi
done

if [[ -n "$GATEWAY_URL" ]]; then
  export OMNIROUTE_BASE_URL="$GATEWAY_URL"
else
  log_warn "No local gateway found on ports 20128 or 20129."
fi

# 4. Run setup: non-interactive if a gateway was detected, interactive otherwise.
if [[ -n "${OMNIROUTE_BASE_URL:-}" ]]; then
  log_info "Running: verdict setup --non-interactive"
  verdict setup --non-interactive
else
  log_info "Running interactive setup: verdict setup"
  verdict setup
fi

# 5. Verify the install.
log_info "Running: verdict check"
if verdict check; then
  log_success "verdict check passed."
else
  log_error "verdict check failed. Review the output above."
  exit 1
fi

# 6. Success message.
echo ""
log_success "Verdict is ready. Try: verdict route 'your task here' --criticality medium"
