#!/usr/bin/env bash
# verdict-core/install.sh — self-contained, idempotent setup script.
#
# Installs a PINNED, integrity-verified release of verdict-core from PyPI.
#
# Quick start:
#   bash <(curl -fsSL https://raw.githubusercontent.com/mrnicholasbcarter-code/verdict-core/main/install.sh)
# From a local checkout:
#   bash install.sh
#
# Override the version:
#   VERDICT_VERSION=0.4.0 bash install.sh

set -euo pipefail

# ── Pinned version & expected hashes ────────────────────────────────
VERDICT_VERSION="${VERDICT_VERSION:-0.4.0}"
# SHA-256 digests from PyPI JSON API for verdict-core 0.4.0
VERDICT_WHL_SHA256="17197fc61cbb97199561956ad9189c7d4b05d3753c2403daa9a7842a13432453"
VERDICT_SDIST_SHA256="ae248ca948aa361a847e5e61e6c87b2ff8b803b6a68710ea6f7082da36bcd73c"

# ── Colours ─────────────────────────────────────────────────────────
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
log_success() { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── Helper: fetch PyPI digest for an exact version ──────────────────
# Usage: pypi_sha256 <version> <packagetype>
# packagetype is "bdist_wheel" or "sdist"
pypi_sha256() {
  local ver="$1" pkgtype="$2"
  local api_url="https://pypi.org/pypi/verdict-core/${ver}/json"
  local api_json
  api_json=$(curl -sf "$api_url") || { log_error "PyPI API request failed for version ${ver}"; return 1; }
  echo "$api_json" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for u in data['urls']:
    if u['packagetype'] == '${pkgtype}':
        print(u['digests']['sha256'])
        sys.exit(0)
print('NOT_FOUND')
sys.exit(1)
"
}

# ── Resolve expected hash ───────────────────────────────────────────
# For the default pinned version use the compiled-in hashes.
# For an overridden version fetch the hash from PyPI first.
resolve_expected_hash() {
  local ver="$1"
  if [[ "$ver" == "0.4.0" ]]; then
    EXPECTED_WHL_SHA256="$VERDICT_WHL_SHA256"
    EXPECTED_SDIST_SHA256="$VERDICT_SDIST_SHA256"
  else
    log_info "Non-default version ${ver} requested; fetching hashes from PyPI..."
    EXPECTED_WHL_SHA256=$(pypi_sha256 "$ver" "bdist_wheel") || {
      log_error "Could not fetch wheel hash for verdict-core==${ver} from PyPI. Aborting."
      exit 1
    }
    EXPECTED_SDIST_SHA256=$(pypi_sha256 "$ver" "sdist") || {
      log_error "Could not fetch sdist hash for verdict-core==${ver} from PyPI. Aborting."
      exit 1
    }
  fi
}

# ── Verify sha256 of a file ────────────────────────────────────────
verify_sha256() {
  local filepath="$1" expected="$2"
  local actual
  actual=$(python3 -c "
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for chunk in iter(lambda: f.read(8192), b''):
        h.update(chunk)
print(h.hexdigest())
" "$filepath")
  if [[ "$actual" != "$expected" ]]; then
    log_error "SHA-256 MISMATCH for $(basename "$filepath")"
    log_error "  expected: ${expected}"
    log_error "  actual:   ${actual}"
    log_error "Artifact may be tampered. Aborting."
    return 1
  fi
  log_success "SHA-256 verified for $(basename "$filepath")"
}

# ── Integrity-verified install ──────────────────────────────────────
install_verified() {
  local ver="$1"
  resolve_expected_hash "$ver"

  local tmpdir
  tmpdir=$(mktemp -d)
  # $tmpdir is local and gone when the EXIT trap runs; under `set -u` that made
  # every fresh install exit 1 after succeeding. Keep the path in a global the
  # trap can read, and quote it at run time so any path is safe.
  VERDICT_INSTALL_TMPDIR="$tmpdir"
  trap 'rm -rf -- "${VERDICT_INSTALL_TMPDIR:-}"' EXIT

  # Download wheel only (no deps) for hash verification
  log_info "Downloading verdict-core==${ver} wheel for verification..."
  pip download --no-deps --dest "$tmpdir" \
    "verdict-core==${ver}" 2>&1 || {
      log_error "Failed to download verdict-core==${ver}. Aborting."
      exit 1
    }

  # Find the downloaded artifact and verify its hash
  local whl sdist artifact expected_hash
  whl=$(find "$tmpdir" -name "verdict_core-*.whl" 2>/dev/null | head -1)
  sdist=$(find "$tmpdir" -name "verdict_core-*.tar.gz" 2>/dev/null | head -1)

  if [[ -n "$whl" ]]; then
    artifact="$whl"
    expected_hash="$EXPECTED_WHL_SHA256"
  elif [[ -n "$sdist" ]]; then
    artifact="$sdist"
    expected_hash="$EXPECTED_SDIST_SHA256"
  else
    log_error "No wheel or sdist found after download. Aborting."
    exit 1
  fi

  verify_sha256 "$artifact" "$expected_hash" || exit 1

  # Install from the verified local artifact
  if command -v pipx >/dev/null 2>&1; then
    log_info "Installing verified artifact via pipx..."
    pipx install "$artifact"
  else
    log_info "Installing verified artifact via pip --user..."
    pip install --user "$artifact"
  fi
}

# ── 1. Install ──────────────────────────────────────────────────────
if command -v verdict >/dev/null 2>&1; then
  log_info "verdict is already installed ($(command -v verdict)); skipping install step."
else
  install_verified "$VERDICT_VERSION"
fi

# ── 2. Detect PATH ─────────────────────────────────────────────────
if ! command -v verdict >/dev/null 2>&1; then
  log_warn "The 'verdict' command was not found on your PATH."
  echo "Add ~/.local/bin to your PATH: export PATH=\$PATH:\$HOME/.local/bin"
  export PATH="$PATH:$HOME/.local/bin"
fi

if ! command -v verdict >/dev/null 2>&1; then
  log_error "verdict is still not on PATH after adding ~/.local/bin. Aborting."
  exit 1
fi

# ── 3. Probe local gateway ─────────────────────────────────────────
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

# ── 4. Run setup ───────────────────────────────────────────────────
if [[ -n "${OMNIROUTE_BASE_URL:-}" ]]; then
  log_info "Running: verdict setup --non-interactive"
  verdict setup --non-interactive
else
  log_info "Running interactive setup: verdict setup"
  verdict setup
fi

# ── 5. Verify the install ──────────────────────────────────────────
# The offline demo needs no key, gateway or config file, so it proves the
# installed package works. `verdict setup --non-interactive` only plans (it
# writes nothing without --apply/--allow), so a config file may not exist yet.
log_info "Running: verdict demo --speed 0 (offline, no model calls)"
if verdict demo --speed 0; then
  log_success "verdict demo completed."
else
  log_error "verdict demo failed. Review the output above."
  exit 1
fi

CONFIG_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/verdict/verdict.yaml"
if [[ -f "$CONFIG_FILE" ]]; then
  log_info "Running: verdict check"
  if verdict check; then
    log_success "verdict check passed."
  else
    log_error "verdict check failed. Review the output above."
    exit 1
  fi
else
  log_warn "No configuration file yet ($CONFIG_FILE)."
  echo "Create one with: verdict setup   (then run: verdict check)"
fi

# ── 6. Success ─────────────────────────────────────────────────────
echo ""
log_success "Verdict is ready. Try: verdict demo"
