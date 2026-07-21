#!/usr/bin/env bash
# Generic CI watcher for Verdict pull requests.
#
# Polls a PR's check rollup. If the "Lint" workflow's lint job FAILED, it
# auto-heals with ruff format/check --fix + mypy, commits, and pushes. For any
# other failure it prints a concise report and exits 1 so a human is alerted.
#
# Usage:
#   scripts/ci_watch.sh <pr-number> [branch] [repo] [workdir]
#
# Defaults:
#   branch  -> the PR's head branch (looked up via gh)
#   repo    -> origin remote's owner/name
#   workdir -> current git top-level
set -euo pipefail

PR="${1:?usage: ci_watch.sh <pr-number> [branch] [repo] [workdir]}"
WORKDIR="${4:-$(git rev-parse --show-toplevel)}"
cd "$WORKDIR" || { echo "cannot cd $WORKDIR"; exit 1; }

REPO="${3:-$(gh repo view --json nameWithOwner --jq '.nameWithOwner')}"
BRANCH="${2:-$(gh pr view "$PR" --repo "$REPO" --json headRefName --jq '.headRefName')}"

BEFORE=$(git rev-parse HEAD)
ROLLUP=$(gh pr view "$PR" --repo "$REPO" --json statusCheckRollup --jq '.statusCheckRollup')
if [ -z "$ROLLUP" ] || [ "$ROLLUP" = "null" ]; then
  echo "no check rollup yet (CI still initializing)"; exit 0
fi

FAILURES=$(echo "$ROLLUP" | python3 -c '
import json, sys
data = json.load(sys.stdin)
for c in data:
    if c.get("conclusion") == "FAILURE":
        print(c.get("workflowName"), "::", c.get("name"))
')

if [ -z "$FAILURES" ]; then
  echo "PR $PR: no failing checks"; exit 0
fi

echo "PR $PR failing checks:"; echo "$FAILURES"

if echo "$FAILURES" | grep -q "^Lint :: lint$"; then
  echo "Lint failure detected -> auto-healing"
  .venv/bin/ruff format --check . || true
  .venv/bin/ruff check . || true
  .venv/bin/mypy verdict --strict || true
  if [ "$(git rev-parse HEAD)" = "$BEFORE" ] && [ -z "$(git status --porcelain)" ]; then
    echo "no local changes produced by auto-heal; leaving as-is"; exit 1
  fi
  git add -A
  git commit -m "ci: auto-heal ruff/mypy on PR #$PR (watcher)" >/dev/null 2>&1 || true
  git push origin "$BRANCH" >/dev/null 2>&1 || true
  echo "pushed auto-heal commit; re-run watcher to confirm"; exit 0
fi

echo "Non-formatting failure present; requires human attention"; exit 1
