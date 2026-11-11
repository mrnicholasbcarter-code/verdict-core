#!/usr/bin/env bash
# BOD-416 acceptance: `verdict route` succeeds with the gateway key ONLY in the
# owner-only key file (no credential store, no key env vars, fresh catalog).
#
# Usage: bod416_keyfile_route.sh <raw-output-file>
# Prints a transcript with no host paths. Temporary copies are shredded/removed.
set -u
OUT=${1:?raw output path}
CFG="$HOME/.config/verdict"
T=$(mktemp -d /tmp/bod416-dod-XXXXXX)
cleanup() { [ -f "$T/.config/verdict/omniroute-api-key" ] && shred -u "$T/.config/verdict/omniroute-api-key"; rm -rf "$T"; }
trap cleanup EXIT
mkdir -p "$T/.config/verdict"
chmod 700 "$T" "$T/.config/verdict"
cp -p "$CFG/verdict.yaml" "$CFG/omniroute-api-key" "$T/.config/verdict/"
rsync -a --exclude 'catalog-cache*.json' --exclude 'census-catalog-cache.json' \
  --exclude '*.db' --exclude 'vgate' "$HOME/.verdict/" "$T/state/"
TASK="Write a Python function that returns the sum of two integers."
# The build under test is the installed one (what the systemd units run), not the dev checkout.
VBIN="${VERDICT_BIN:-$HOME/.local/share/verdict/installed-main/.venv/bin/verdict}"
PY="$(dirname "$VBIN")/python"
PKG_DIR="$("$PY" -I -c 'import os, verdict; print(os.path.dirname(verdict.__file__))')"
SRC_ROOT="$(dirname "$PKG_DIR")"
{
  echo "bod416-keyfile-route at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "installed_commit=$(git -C "$SRC_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
  echo "installed_tree_clean=$([ -z "$(git -C "$SRC_ROOT" status --porcelain -- verdict 2>/dev/null)" ] && echo yes || echo no)"
  echo "credential_store_present=$([ -e "$T/.config/verdict/credentials.env" ] && echo yes || echo no)"
  echo "key_file_present=$([ -s "$T/.config/verdict/omniroute-api-key" ] && echo yes || echo no)"
  echo "catalog_cache_before=$([ -e "$T/state/catalog-cache.json" ] && echo yes || echo no)"
  echo "command: env -i PATH=... HOME=<tmp> XDG_CONFIG_HOME=<tmp>/.config VERDICT_HOME=<tmp>/state verdict route --terse \"$TASK\""
  cd /tmp
  env -i PATH="$HOME/.local/bin:/usr/bin:/bin" HOME="$T" XDG_CONFIG_HOME="$T/.config" \
    VERDICT_HOME="$T/state" VERDICT_RECEIPTS_DB="$T/state/r.db" LANG=C.UTF-8 \
    env | grep -c -E '^(OMNIROUTE_API_KEY|VERDICT_OMNIROUTE_API_KEY)=' | sed 's/^/key_env_vars_in_child=/'
  env -i PATH="$HOME/.local/bin:/usr/bin:/bin" HOME="$T" XDG_CONFIG_HOME="$T/.config" \
    VERDICT_HOME="$T/state" VERDICT_RECEIPTS_DB="$T/state/r.db" LANG=C.UTF-8 \
    "$VBIN" route --terse "$TASK" >"$T/out.txt" 2>"$T/err.txt"
  rc=$?
  echo "route_stdout=$(sed "s|$T|<tmp>|g" "$T/out.txt")"
  echo "route_stderr_bytes=$(wc -c <"$T/err.txt")"
  echo "credential_env_missing_count=$(grep -c credential_env_missing "$T/err.txt")"
  "$PY" -I -c '
import json, sys
d = json.load(open(sys.argv[1]))
entries = d.get("entries") or {}
print(f"catalog_cache_after_entries={len(entries)}")
' "$T/state/catalog-cache.json" 2>/dev/null || echo "catalog_cache_after_entries=0"
  echo "gateway=$(grep -m1 -o -E 'http://127\.0\.0\.1:20128' "$T/.config/verdict/verdict.yaml" || echo unknown)"
  echo "route_exit=$rc"
  if [ "$rc" -eq 0 ] && ! grep -q no_eligible_target "$T/out.txt"; then
    echo "RESULT: routed with the gateway key from the key file only"
  else
    echo "RESULT: FAILED"
  fi
} >"$OUT"
grep -q '^RESULT: routed' "$OUT"
