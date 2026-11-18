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
#   VERDICT_VERSION=0.4.2 bash install.sh
#
# Install from a local source tree (bypasses PyPI download and hash checks):
#   VERDICT_SOURCE=/path/to/verdict-core bash install.sh
#
# Force a specific installer (for testing):
#   VERDICT_INSTALLER=uv|pipx|venv bash install.sh

set -euo pipefail

# ── Pinned version & expected hashes ────────────────────────────────
# Policy: this default pin is updated only AFTER a release is published on
# PyPI, using real digests pulled from the PyPI JSON API (see pypi_sha256()
# below) — never invented or pre-computed ahead of publication. That is why
# this stays at 0.4.2 until the next release (0.5.0) actually exists on
# PyPI; an override via VERDICT_VERSION already fetches its own hashes
# unpinned.
VERDICT_VERSION="${VERDICT_VERSION:-0.4.2}"
# SHA-256 digests from PyPI JSON API for verdict-core 0.4.2
VERDICT_WHL_SHA256="202496d4235d9b810d5f62e4b31e364809df9cc2333ce039d6637ab6cd2bb15d"
VERDICT_SDIST_SHA256="b59e3858b4f29ca65f7261fcdbf3ec456bc2116885542af81407b9284484745c"

# Optional: install from a local source checkout (skips PyPI download + hash check)
VERDICT_SOURCE="${VERDICT_SOURCE:-}"

# Optional: force a specific installer (uv, pipx, venv)
VERDICT_INSTALLER="${VERDICT_INSTALLER:-}"

# ── Colours ─────────────────────────────────────────────────────────
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}[INFO]${NC} $*" >&2; }
log_success() { echo -e "${GREEN}[OK]${NC} $*" >&2; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
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
  if [[ "$ver" == "0.4.2" ]]; then
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

# ── Install uv if not present ──────────────────────────────────────
# Uses the official uv installer (https://github.com/astral-sh/uv#installation)
ensure_uv() {
  if [[ "$VERDICT_INSTALLER" == "pipx" ]] || [[ "$VERDICT_INSTALLER" == "venv" ]]; then
    return 1
  fi
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  log_info "uv not found; installing uv via official installer..."
  local uv_installer="https://astral.sh/uv/install.sh"
  if curl -fsSL "$uv_installer" | sh; then
    # uv installer puts binary in ~/.local/bin or ~/.cargo/bin
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then
      log_success "uv installed successfully"
      return 0
    fi
  fi
  log_warn "uv installation failed or uv not on PATH after install"
  return 1
}

# ── Install via uv tool ────────────────────────────────────────────
install_via_uv() {
  local artifact="$1"
  if [[ "$VERDICT_INSTALLER" == "pipx" ]] || [[ "$VERDICT_INSTALLER" == "venv" ]]; then
    return 1
  fi
  log_info "Installing verified artifact via uv tool..."
  # uv tool install supports installing from a local wheel file
  if uv tool install "$artifact"; then
    log_success "Installed via uv tool"
    return 0
  fi
  log_warn "uv tool install failed"
  return 1
}

# ── Install via pipx ───────────────────────────────────────────────
install_via_pipx() {
  local artifact="$1"
  if [[ "$VERDICT_INSTALLER" == "uv" ]] || [[ "$VERDICT_INSTALLER" == "venv" ]]; then
    return 1
  fi
  if ! command -v pipx >/dev/null 2>&1; then
    return 1
  fi
  log_info "Installing verified artifact via pipx..."
  if pipx install "$artifact"; then
    log_success "Installed via pipx"
    return 0
  fi
  log_warn "pipx install failed"
  return 1
}

# ── Install via private venv with symlink ──────────────────────────
# Creates a dedicated virtual environment under ~/.local/share/verdict-core/venv
# and symlinks the verdict binary to ~/.local/bin/verdict
install_via_private_venv() {
  local artifact="$1"
  local venv_dir="${HOME}/.local/share/verdict-core/venv"
  local bin_dir="${HOME}/.local/bin"
  local verdict_bin="${venv_dir}/bin/verdict"
  local symlink_target="${bin_dir}/verdict"

  log_info "Installing verified artifact via private venv (${venv_dir})..."

  # Create the venv
  python3 -m venv "$venv_dir" || {
    log_error "Failed to create virtual environment at ${venv_dir}"
    return 1
  }

  # Install the wheel into the venv (with dependencies)
  "${venv_dir}/bin/pip" install "$artifact" || {
    log_error "Failed to install artifact into private venv"
    return 1
  }

  # Ensure ~/.local/bin exists
  mkdir -p "$bin_dir"

  # Create/update symlink
  ln -sfn "$verdict_bin" "$symlink_target" || {
    log_error "Failed to create symlink ${symlink_target} -> ${verdict_bin}"
    return 1
  }

  log_success "Installed via private venv; symlinked to ${symlink_target}"
  return 0
}

# ── Build wheel from local source using private venv ───────────────
build_wheel_from_source() {
  local source_dir="$1"
  local output_dir="$2"
  local venv_dir="${HOME}/.local/share/verdict-core/build-venv"

  log_info "Building wheel from local source using isolated build venv..."

  # Create a temporary build venv
  python3 -m venv "$venv_dir" || {
    log_error "Failed to create build virtual environment"
    return 1
  }

  # Install build and hatchling in the build venv
  "${venv_dir}/bin/pip" install --quiet build hatchling || {
    log_error "Failed to install build dependencies in build venv"
    rm -rf "$venv_dir"
    return 1
  }

  # Build the wheel - disable color output to avoid ANSI codes in pip input
  local artifact
  NO_COLOR=1 TERM=dumb artifact=$(cd "$source_dir" && "${venv_dir}/bin/python" -m build --wheel --no-isolation -o "$output_dir" 2>&1)
  artifact=$(find "$output_dir" -name "verdict_core-*.whl" 2>/dev/null | head -1)

  # Clean up build venv
  rm -rf "$venv_dir"

  if [[ -z "$artifact" ]]; then
    log_error "Failed to build wheel from local source"
    return 1
  fi

  echo "$artifact"
  return 0
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

  local artifact

  if [[ -n "$VERDICT_SOURCE" ]]; then
    # Install from local source tree - build wheel in isolated venv
    log_info "Building wheel from local source: ${VERDICT_SOURCE}"
    artifact=$(build_wheel_from_source "$VERDICT_SOURCE" "$tmpdir") || exit 1
    log_info "Built wheel: $(basename "$artifact")"
    # Skip hash verification for local builds
  else
    # Download wheel only (no deps) for hash verification
    log_info "Downloading verdict-core==${ver} wheel for verification..."
    pip download --no-deps --dest "$tmpdir" \
      "verdict-core==${ver}" 2>&1 || {
      log_error "Failed to download verdict-core==${ver}. Aborting."
      exit 1
    }

    # Find the downloaded artifact and verify its hash
    local whl sdist expected_hash
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
  fi

  # Try installation methods in order of preference
  # 1. uv tool install (preferred: handles PEP 668, isolated environments)
  if ensure_uv && install_via_uv "$artifact"; then
    return 0
  fi

  # 2. pipx (good: isolated environments, but may not be installed)
  if install_via_pipx "$artifact"; then
    return 0
  fi

  # 3. Private venv with symlink (always works, no external dependencies)
  if install_via_private_venv "$artifact"; then
    return 0
  fi

  log_error "All installation methods failed. Aborting."
  exit 1
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