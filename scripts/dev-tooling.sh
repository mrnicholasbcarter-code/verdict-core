#!/usr/bin/env bash
# scripts/dev-tooling.sh - install and verify the recommended contributor tooling.
#
# Required for every contributor: uv (Python env, see dev-start.sh) and git.
# Recommended (this script): fast search, code-structure search, quality, hygiene and security
# scanners that Verdict's own reviews and gates use. Nothing here is a runtime dependency.
#
#   scripts/dev-tooling.sh            # check: report installed/missing, exit 0
#   scripts/dev-tooling.sh --install  # install missing tools (Homebrew/Linuxbrew + uv tool)
#   scripts/dev-tooling.sh --strict   # check: exit 1 if any recommended tool is missing
set -euo pipefail

MODE="check"
case "${1:-}" in
  --install) MODE="install" ;;
  --strict) MODE="strict" ;;
  ""|--check) MODE="check" ;;
  -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

# name|command|installer|package|purpose
TOOLS=(
  "ripgrep|rg|brew|ripgrep|fast text search"
  "fd|fd|brew|fd|fast file search"
  "jq|jq|brew|jq|JSON processing"
  "gh|gh|brew|gh|GitHub CLI (PRs, checks, API) - preferred over raw REST"
  "ast-grep|sg|brew|ast-grep|structural code search and rewrite"
  "semgrep|semgrep|brew|semgrep|static analysis rules"
  "shellcheck|shellcheck|brew|shellcheck|shell script linting"
  "actionlint|actionlint|brew|actionlint|GitHub Actions workflow linting"
  "gitleaks|gitleaks|brew|gitleaks|secret scanning"
  "typos|typos|brew|typos-cli|source spell checking"
  "codespell|codespell|brew|codespell|docs spell checking"
  "lychee|lychee|brew|lychee|link checking for docs"
  "vulture|vulture|brew|vulture|dead code detection (Python)"
  "deptry|deptry|uv|deptry|unused/missing dependency detection (Python)"
  "openspec|openspec|npm|@fission-ai/openspec@1.13.2|OpenSpec change validation"
)

have() { command -v "$1" >/dev/null 2>&1; }

install_tool() {
  local installer="$1" package="$2"
  case "$installer" in
    brew)
      have brew || { echo "  brew not found; install Homebrew/Linuxbrew or use your package manager" >&2; return 1; }
      HOMEBREW_NO_AUTO_UPDATE=1 brew install "$package" ;;
    uv)
      have uv || { echo "  uv not found; see https://docs.astral.sh/uv/" >&2; return 1; }
      uv tool install "$package" ;;
    npm)
      have npm || { echo "  npm not found; install Node.js 18+" >&2; return 1; }
      npm install -g "$package" ;;
  esac
}

missing=0
printf '%-11s %-8s %s\n' "TOOL" "STATUS" "PURPOSE"
for row in "${TOOLS[@]}"; do
  IFS='|' read -r name cmd installer package purpose <<<"$row"
  if have "$cmd"; then
    printf '%-11s %-8s %s\n' "$name" "ok" "$purpose"
    continue
  fi
  if [[ "$MODE" == "install" ]]; then
    echo "installing $name ($installer: $package)"
    if install_tool "$installer" "$package" && have "$cmd"; then
      printf '%-11s %-8s %s\n' "$name" "ok" "$purpose"
      continue
    fi
  fi
  printf '%-11s %-8s %s\n' "$name" "MISSING" "$purpose ($installer: $package)"
  missing=$((missing + 1))
done

if (( missing > 0 )); then
  echo "$missing recommended tool(s) missing. Run: scripts/dev-tooling.sh --install"
  [[ "$MODE" == "strict" ]] && exit 1
fi
exit 0
