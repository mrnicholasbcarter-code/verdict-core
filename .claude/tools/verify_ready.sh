#!/bin/bash
# Is a PR ready for the operator's merge decision? One deterministic answer, no model in the loop.
# Usage: verify_ready.sh <pr-number>
# Ready means, all on the PR's CURRENT head commit:
#   1. the stored cross-family review record reviews/<full-sha><suffix>.json is complete, covers every
#      selected file, has 0 findings, 0 failed reviewer tool calls and differs from the author's family;
#   2. at least one check run exists and every check run is completed with success, skipped or neutral.
# REVIEW_RECORD_SUFFIX picks the record (default ".independent", the story-pipeline workflow's own review;
# set it to "" for a PR that was not built by the workflow).
# Prints one line "READY ..." and exits 0, or "NOT READY ..." with every reason and exits 1.
# Read-only: it never reviews, pushes or merges.
set -euo pipefail
PR="$1"
SUFFIX="${REVIEW_RECORD_SUFFIX-.independent}"
# REPO is the checkout this script is versioned in. OUT is the machine-local pipeline state directory
# that review_pr.sh writes its review records to; it is not versioned.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT=/tmp/claude-takeover/reviews
# Builders use Codex unless a caller supplies a different author family.
source "$REPO/.claude/tools/review_pr.sh"
AUTHOR_FAMILY="${REVIEW_AUTHOR_FAMILY:-codex}"
AUTHOR_FAMILY="${AUTHOR_FAMILY,,}"
case "$AUTHOR_FAMILY" in
  codex|claude|gemini) ;;
  *) echo "NOT READY invalid REVIEW_AUTHOR_FAMILY '$AUTHOR_FAMILY' (codex, claude or gemini)"; exit 1;;
esac
cd "$REPO"
SHA=$(gh pr view "$PR" --json headRefOid --jq .headRefOid)
STATE=$(gh pr view "$PR" --json state --jq .state)
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
gh api "repos/{owner}/{repo}/commits/$SHA/check-runs?per_page=100" > "$TMP"
REC="$OUT/$SHA$SUFFIX.json"
MODEL=""
if [ -s "$REC" ]; then
  if ! MODEL=$(python3 -I -c 'import json,sys; print((json.load(open(sys.argv[1])).get("llm") or {}).get("model") or "")' "$REC" 2>/dev/null); then
    echo "NOT READY pr=$PR head=$SHA :: unreadable review record"
    exit 1
  fi
fi
FAMILY=$(review_family "$MODEL")
python3 -I - "$PR" "$SHA" "$STATE" "$REC" "$TMP" "$AUTHOR_FAMILY" "$FAMILY" <<'EOF'
import json, os, sys
pr, sha, state, rec, checks_file, author_family, reviewer_family = sys.argv[1:8]
reasons = []
if state != "OPEN":
    reasons.append(f"pull request is {state}")
review = "none"
if not os.path.isfile(rec) or os.path.getsize(rec) == 0:
    reasons.append(f"no review record for this head ({os.path.basename(rec)})")
else:
    d = json.load(open(rec))
    cov = (d.get("manifest") or {}).get("coverage") or {}
    done, sel = len(cov.get("completed") or []), len(cov.get("selected") or [])
    findings = len(d.get("comments") or [])
    tc = d.get("tool_calls") if isinstance(d.get("tool_calls"), dict) else {}
    failed = int(tc.get("failure") or 0)
    model = str((d.get("llm") or {}).get("model") or "")
    review = (f"{d.get('status')} files={done}/{sel} findings={findings} "
              f"tool_calls={int(tc.get('total') or 0)} tool_failures={failed} model={model}")
    if d.get("status") != "complete":
        reasons.append(f"review status is {d.get('status')}")
    if sel == 0 or done != sel:
        reasons.append(f"review covered {done} of {sel} files")
    if findings:
        reasons.append(f"review has {findings} finding(s)")
    if failed:
        reasons.append(f"{failed} reviewer tool call(s) failed")
    if not model:
        reasons.append("reviewer model is missing")
    elif reviewer_family == author_family:
        reasons.append(f"reviewer model {model!r} is the same family as author ({author_family})")
    elif reviewer_family.startswith("unknown:"):
        review += f" reviewer_family={reviewer_family} (unknown prefix treated as different)"
runs = json.load(open(checks_file)).get("check_runs", [])
bad = sorted({r.get("name") for r in runs
              if r.get("status") != "completed" or r.get("conclusion") not in ("success", "skipped", "neutral")})
if not runs:
    reasons.append("no check runs on this head")
if bad:
    reasons.append(f"{len(bad)} check(s) not green: " + ", ".join(bad[:8]))
line = f"pr={pr} head={sha} review=[{review}] checks_not_green={len(bad)} of {len(runs)}"
if reasons:
    print("NOT READY " + line + " :: " + "; ".join(reasons))
    sys.exit(1)
print("READY " + line)
EOF
