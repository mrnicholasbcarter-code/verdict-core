#!/bin/bash
# PreToolUse(Bash): block `git stash` in this project (operator rule: no git stash
# across worktrees; the stash stack is shared by every worktree, and a stray
# `stash pop` conflicted the main checkout on 2026-11-23 twice).
# `git stash list` and `git stash show` are read-only and stay allowed.
# This file is versioned; the registration is not. Register it per machine in .claude/settings.local.json
# (git-ignored):
#   {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
#     "command": "\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/block-git-stash.sh"}]}]}}
cmd=$(jq -r '.tool_input.command // ""' 2>/dev/null)
if printf '%s' "$cmd" | grep -Pq '(^|[;&|(`]\s*|\b(then|do|else)\s+)git\b(\s+-[^\s]+(\s+[^\s-]\S*)?)*\s+stash\b(?!\s+(list|show)\b)'; then
  jq -n '{hookSpecificOutput:{hookEventName:"PreToolUse",permissionDecision:"deny",permissionDecisionReason:"git stash is blocked in verdict-core: the stash stack is shared by all worktrees. To check whether a failure is pre-existing, run the test in a detached worktree of origin/main (git worktree add --detach /tmp/base-check origin/main) or commit a WIP commit first. git stash list/show are allowed."}}'
fi
exit 0
