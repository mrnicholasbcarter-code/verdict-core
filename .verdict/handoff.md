# Verdict Handoff

Story: BOD-124 (harness-prime-opencode follow-through)
Worker/harness: cursor
Worktree: /home/nick/worktrees/bod-124-harness-prime-opencode
Branch: feat/bod-124-harness-prime-opencode
Base SHA: origin/main @ 9926bde (#553 claude/cursor included)
Previous worker: none
Objective: Phase-4 harness adapters for Prime Agent + OpenCode (discover/enable/disable/status/certify) mirroring codex/hermes/claude/cursor patterns.

Completed:
- Implemented `verdict/harness_prime.py` and `verdict/harness_opencode.py`
- CLI: `verdict harness prime|opencode {discover,enable,disable,status,certify}`
- Docs: `docs/guides/prime-harness.md`, `opencode-harness.md`; coding-agent-gate updated
- Temp-dir unit tests (12); refuse OmniRoute `:20128` without `--force`; default Verdict `:8000`; backup+rollback
- Certify reports `not-installed` when binaries missing; live enable NEEDS_OWNER
- ruff + mypy --strict clean on new modules

Currently working on:
- (none — awaiting PR review/merge)

Next exact steps:
1. Review/merge PR for feat/bod-124-harness-prime-opencode
2. Live enable with secrets remains NEEDS_OWNER

Acceptance criteria:
- [x] discover/status/enable/disable/certify for prime + opencode
- [x] Graceful not-installed when prime/opencode/opencode-go missing
- [x] Unit tests do not require binaries installed
- [x] Refuse :20128 without --force; default :8000; backup+rollback
- [x] ruff/mypy clean

Tests/proof executed:
- `uv run --extra dev pytest tests/test_harness_prime.py tests/test_harness_opencode.py -q` (12 passed)
- `uv run --extra dev ruff check/format` on new modules + tests
- `uv run --extra dev mypy --strict verdict/harness_prime.py verdict/harness_opencode.py`

Files changed:
- verdict/harness_prime.py (new)
- verdict/harness_opencode.py (new)
- verdict/cli.py (prime/opencode harness parsers + cmds)
- tests/test_harness_prime.py (new)
- tests/test_harness_opencode.py (new)
- docs/guides/prime-harness.md (new)
- docs/guides/opencode-harness.md (new)
- docs/guides/coding-agent-gate.md
- .verdict/handoff.md

Contracts changed:
- Architect-locked CLI surfaces for prime + opencode harness adapters

Important decisions:
- Rebased onto #553; CLI parsers for prime/opencode sit alongside claude/cursor/codex/hermes
- Parity includes `not-installed` when binary absent
- Prime writes `~/.prime/agent/models.json`; OpenCode writes `~/.config/opencode/opencode.json`
- Never write token values; live enable NEEDS_OWNER

Known failures:
- (none)

Dependencies/blockers:
- Live enable with secrets is NEEDS_OWNER
- Do not touch delivery.py, compaction.py, capacity_*, optimized_dispatch.py

Do not / warnings:
- Do not rewrite claude/cursor/codex/hermes modules
- Do not force-push main
