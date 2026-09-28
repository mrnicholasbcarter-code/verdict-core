# Harness Independence Proof — 2026-09-28

## What this proves

The same orchestration run-view (verdict/orchestration/tui.py) renders runs
executed through two different WorkerExecutor implementations:

1. **PrimeHeadlessExecutor** (`--executor prime`) — runs prompts through the
   `prime-agent` headless CLI.
2. **DirectGatewayExecutor** (`--executor direct-gateway`) — calls OmniRoute's
   OpenAI-compatible `/v1/chat/completions` endpoint directly via HTTP (no
   Prime process).

Both runs target the same goal ("Add a docstring to the greet function in
hello.py") in a throwaway git repo under `/tmp`.

## Artifacts

| Directory               | Executor         | Run ID               | Outcome  | Integrity |
|------------------------|-----------------|---------------------|----------|-----------|
| `prime-run/`           | prime            | 20260928T184622Z     | BLOCKED (review MISSING) | OK |
| `direct-gateway-run/`  | direct-gateway   | 20260928T184458Z  | BLOCKED (verification_failed) | OK |

Each directory contains:
- `events.jsonl` — the append-only event stream
- `receipt.json` — the run receipt with SHA-256 event-log digest

## Verification commands

```bash
# Verify receipt integrity
verdict run-receipt docs/proof/harness-independence-2026-09-28/prime-run
verdict run-receipt docs/proof/harness-independence-2026-09-28/direct-gateway-run
```

## What it shows

- Both executors produce valid event streams consumable by `RunView.from_events()`.
- Both event streams render through `render()` / `render_text()` with identical
  section structure: GOAL, UNDERSTAND, CONTROLLER, PLAN/DAG, SELECT, HYDRATE,
  WORKERS, QUOTA/COOLDOWN, FAILURE/REASSIGN, VERIFY, REVIEW.
- Receipt schema is identical (`verdict.run-receipt/v1`).
- Event-log digest is verified for both runs.

## What it does NOT show

- The direct-gateway executor does not apply diffs to the working tree (it
  returns the model's text verbatim). Implement-type nodes fail verification
  because no file changes are made. This is expected: the DirectGatewayExecutor
  is designed for text-only nodes (research/review/plan) and serves as a proof
  that the orchestration harness is executor-agnostic.
- The Prime run also ended BLOCKED because `--no-review` was used and the
  receipt requires review status PASS.

## Models used

- Planner: `cc/claude-haiku-4-5-20251001`
- Workers: `cc/claude-sonnet-4-5-20250929`, `cc/claude-sonnet-4-6` (selected by
  the eligibility ladder)
