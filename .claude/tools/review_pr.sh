#!/bin/bash
# Cross-family review of a PR's exact head with OpenCodeReview (ocr).
# Usage: ~/.config/verdict/with-new-gateway-key.sh .claude/tools/review_pr.sh <pr-number | worktree-path> "<what the change is and what to check>" [model]
#   Run it through the key wrapper: a shell that was started before the gateway key was rotated holds
#   the old key, and the review then fails with HTTP 401 (2026-11-28). The wrapper supplies the current
#   key to the command without printing it. This script never prints the key.
#   With a worktree path, reviews that worktree's committed HEAD before it is pushed
#   (the tree must be clean). Records are keyed by commit SHA, so a head reviewed
#   locally and then pushed unchanged is not reviewed again.
# - Reviews the PR head in a throwaway detached worktree (never an agent's worktree).
# - Scope = files changed against origin/main.
# - Records the result under reviews/<sha><suffix>.json; a head that was already
#   reviewed to completion under the same suffix is not reviewed again (prints the stored result).
# - REVIEW_RECORD_SUFFIX (default empty) keeps a second, separate record for the same head.
#   The story-pipeline workflow sets it to ".independent" so its review is its own, with its own
#   brief, and never the record the author made before pushing.
# - REVIEW_MODEL selects the default reviewer (cc/claude-sonnet-5-5); argument 3 overrides it.
# - REVIEW_AUTHOR_FAMILY (codex, claude, gemini) refuses that family, including cached reviews
#   and fallback attempts. Unknown route prefixes are named and treated as different families.
# - A missing, empty or incomplete review exits non-zero. It is never a pass.
# - A review in which any reviewer tool call failed (for example code search) exits non-zero too:
#   the reviewer could not look at what it asked for. (Added 2026-11-24: under git 2.34 every
#   code_search call failed and the reviews still counted as complete.)
# Route-prefix aliases live in one table, also sourced by verify_ready.sh.
review_family() {
  local route="${1,,}"
  case "${route%%/*}" in
    cx|codex) printf '%s\n' codex;;
    cc|claude) printf '%s\n' claude;;
    agy|antigravity) printf '%s\n' gemini;;
    *) printf 'unknown:%s\n' "${1%%/*}";;
  esac
}
# Sourcing exposes only the family table, without starting a review.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then return 0; fi
set -euo pipefail

PR="$1"; CONTEXT="$2"; MODEL="${3:-${REVIEW_MODEL:-cc/claude-sonnet-5-5}}"
AUTHOR_FAMILY="${REVIEW_AUTHOR_FAMILY:-}"
AUTHOR_FAMILY="${AUTHOR_FAMILY,,}"
case "$AUTHOR_FAMILY" in
  ''|codex|claude|gemini) ;;
  *) echo "invalid REVIEW_AUTHOR_FAMILY '$AUTHOR_FAMILY' (codex, claude or gemini); review not obtained. NOT a pass."; exit 3;;
esac
reviewer_allowed() {
  local family
  if [ -z "$1" ]; then
    echo "reviewer model is missing; review not obtained. NOT a pass."
    return 1
  fi
  family=$(review_family "$1")
  if [ -n "$AUTHOR_FAMILY" ] && [ "$family" = "$AUTHOR_FAMILY" ]; then
    echo "reviewer $1 is the same family as author ($AUTHOR_FAMILY); review not obtained. NOT a pass."
    return 1
  fi
  case "$family" in unknown:*) echo "reviewer $1 family=$family; unknown prefix treated as different from author family ${AUTHOR_FAMILY:-unspecified}";; esac
}
if ! reviewer_allowed "$MODEL"; then exit 3; fi
# Fallbacks are used only when no explicit third argument was named and the default reviewer
# was rate-limited or timed out. Every attempted reviewer must differ from the author's family.
EXPLICIT_MODEL="${3:-}"
FALLBACK_MODEL="${REVIEW_FALLBACK_MODEL:-agy/gemini-3.8-flash-high}"
SUFFIX="${REVIEW_RECORD_SUFFIX:-}"
# REPO is the checkout this script is versioned in (a worktree or the main checkout); the throwaway review
# worktree is created from it. TOOLS holds run_review.py next to this script.
TOOLS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$TOOLS/../.." && pwd)"
# BASE is the machine-local pipeline state directory (review records, logs, throwaway worktrees). It is not
# versioned; verify_ready.sh and the story-pipeline workflow read the same directory.
BASE=/tmp/claude-takeover
OUT="$BASE/reviews"; mkdir -p "$OUT"
# The installed Verdict environment (it provides verdict.credentials_store). VERDICT_PYTHON overrides it.
PY="${VERDICT_PYTHON:-$HOME/.local/share/verdict/installed-main/.venv/bin/python}"

cd "$REPO"
git fetch -q origin
if [ -d "$PR" ]; then
  SRC=$(cd "$PR" && pwd)
  if [ -n "$(git -C "$SRC" status --porcelain)" ]; then
    echo "worktree $SRC has uncommitted changes; commit first (the review is of a commit)"; exit 4
  fi
  SHA=$(git -C "$SRC" rev-parse HEAD)
  PR="local"
else
  SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
fi
REC="$OUT/$SHA$SUFFIX.json"

summarise() {
  local reviewer family
  reviewer=$(python3 -I -c 'import json,sys; print((json.load(open(sys.argv[1])).get("llm") or {}).get("model") or "")' "$REC") || return 3
  reviewer_allowed "$reviewer" || return 3
  family=$(review_family "$reviewer")
  python3 -I - "$REC" "$PR" "$SHA" "$family" <<'EOF'
import json, sys
rec, pr, sha, family = sys.argv[1:5]
d = json.load(open(rec))
cov = (d.get("manifest") or {}).get("coverage") or {}
done, sel = len(cov.get("completed") or []), len(cov.get("selected") or [])
comments = d.get("comments") or []
tc = d.get("tool_calls") if isinstance(d.get("tool_calls"), dict) else {}
calls, failed = int(tc.get("total") or 0), int(tc.get("failure") or 0)
print(f"PR #{pr} head {sha} review={d.get('status')} model={(d.get('llm') or {}).get('model')} files={done}/{sel} findings={len(comments)} tool_calls={calls} tool_failures={failed} reviewer_family={family}")
for c in comments:
    print("  -", c.get("path"), c.get("start_line"), (c.get("content") or "")[:900].replace("\n", " "))
if failed:
    for f in (tc.get("failure_details") or [])[:5]:
        print("  ! reviewer tool call failed:", f.get("tool_name"), str(f.get("error"))[:200])
    print(f"NOT a pass: {failed} reviewer tool call(s) failed, so the reviewer could not look at what it asked for.")
# Fail closed: an incomplete or empty-coverage review, or one with failed tool calls, is not a pass.
sys.exit(0 if d.get("status") == "complete" and sel > 0 and done == sel and not failed else 3)
EOF
}

if [ -s "$REC" ] && summarise >/dev/null 2>&1; then
  echo "(stored result for unchanged head)"
  summarise
  exit 0
fi

WT="$BASE/review-wt-${SHA:0:8}${SUFFIX//./-}"
RULE="$OUT/$SHA$SUFFIX.rule.json"
LOG="$OUT/$SHA$SUFFIX.log"
git worktree add -q --detach "$WT" "$SHA"
trap 'git -C "$REPO" worktree remove --force "$WT" >/dev/null 2>&1 || true' EXIT
# REVIEW_INCLUDE_FILE (added 2026-11-25): a file listing a SUBSET of the changed paths, one per line,
# to review a large change in parts when one run cannot finish. Use a distinct REVIEW_RECORD_SUFFIX
# per part, and check by hand that the parts together cover every changed file (the operator-local
# verify_parts.sh in the pipeline state directory does this; it is not versioned).
git diff --name-only "origin/main...$SHA" | REVIEW_INCLUDE_FILE="${REVIEW_INCLUDE_FILE:-}" python3 -I -c "
import sys,json,os
changed=[l.strip() for l in sys.stdin if l.strip()]
inc=os.environ.get('REVIEW_INCLUDE_FILE')
if inc:
    want=[l.strip() for l in open(inc) if l.strip()]
    bad=[w for w in want if w not in changed]
    if bad: sys.exit('not in the diff: '+', '.join(bad))
    changed=want
print(json.dumps({'include':changed}))" > "$RULE"
run_once() {
  rm -f "$REC"
  ( cd "$WT" && OCR_BUDGET="${OCR_BUDGET:-3000000}" OCR_RULE="$RULE" timeout "${OCR_TIMEOUT:-2400}" \
      "$PY" "$TOOLS/run_review.py" "$WT" origin/main "$SHA" "$1" "$REC" "$CONTEXT" >"$LOG" 2>&1 ) || true
}
rate_limited() {
  grep -qiE 'rate_limited|"status_code": ?429|\b429\b' "$LOG" 2>/dev/null && return 0
  [ -s "$REC" ] && grep -qE '"error_class": ?"rate_limited"' "$REC"
}
# Skip the default reviewer outright when the latest admission snapshot says it is exhausted and the
# cool-down has not passed: on 2026-11-24 an exhausted Codex route hung to the 2300 s timeout
# instead of answering 429, so waiting for the failure cost 40 minutes and left no record.
default_exhausted() {
  # A live one-word probe decides (changed 2026-11-25): the admission snapshot kept saying the
  # Codex routes were exhausted for a day after they were answering again, which sent three builds
  # to fallbacks that could not review. Exhausted means the gateway answers 429 for the model now.
  "$PY" -I - "$MODEL" <<'PYEOF'
import json, sys, urllib.request, urllib.error
try:
    from verdict.credentials_store import resolve_gateway_key
    key = resolve_gateway_key()[0]
    body = {"model": sys.argv[1], "messages": [{"role": "user", "content": "Reply with the single word ok."}], "max_tokens": 16}
    req = urllib.request.Request("http://127.0.0.1:20128/v1/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            sys.exit(1)  # answers: not exhausted
    except urllib.error.HTTPError as exc:
        sys.exit(0 if exc.code == 429 else 1)
except Exception:
    pass
sys.exit(1)
PYEOF
}
# Fallback chain: each reviewer is tried in turn until one gives a complete review. Same order as
# the cross-review plugin; entries from the author's family are skipped.
# gh/* (Copilot) routes are not in the chain: on 2026-11-24 every one answered 400 model_not_supported.
# Fallback runs use ONE subtask at a time and a 12M-token budget: with 2 or 8 in parallel the Gemini routes answered 403 for some groups, and at 3M tokens a 30-file review ran out of budget.
FALLBACK_MODELS="${REVIEW_FALLBACK_MODELS:-$FALLBACK_MODEL gc/grok-4.7}"
try_fallbacks() {
  local n=0
  for m in $FALLBACK_MODELS; do
    if ! reviewer_allowed "$m"; then echo "(fallback reviewer $m skipped)"; continue; fi
    echo "(reviewing with fallback $m)"
    [ -s "$LOG" ] && cp -f "$LOG" "$LOG.attempt$n" 2>/dev/null || true
    [ -s "$REC" ] && cp -f "$REC" "$REC.attempt$n" 2>/dev/null || true
    n=$((n+1))
    OCR_CONCURRENCY="${OCR_CONCURRENCY:-1}" OCR_BUDGET="${OCR_BUDGET:-12000000}" run_once "$m"
    if summarise >/dev/null 2>&1; then return 0; fi
    echo "(fallback $m did not give a complete review)"
  done
  return 0
}
if [ -n "$EXPLICIT_MODEL" ]; then
  run_once "$MODEL"
elif default_exhausted; then
  echo "(default reviewer $MODEL is exhausted in the admission snapshot)"
  try_fallbacks
else
  run_once "$MODEL"
  if ! summarise >/dev/null 2>&1 && { rate_limited || grep -q 'timed out after' "$LOG" 2>/dev/null; }; then
    echo "(default reviewer $MODEL was rate-limited or timed out)"
    try_fallbacks
  fi
fi
if [ ! -s "$REC" ]; then
  echo "PR #$PR head ${SHA:0:8}: review produced no result (see $LOG). NOT a pass."
  exit 2
fi
summarise
